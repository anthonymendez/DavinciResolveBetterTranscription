from __future__ import annotations

import math
import os
import shutil
import tarfile
import urllib.request
from collections.abc import Hashable
from pathlib import Path, PurePosixPath
from typing import Callable

import numpy as np

from better_captions.paths import models_dir
from better_captions.schema import SpeakerTurn

ProgressFn = Callable[[float], None]

SAMPLE_RATE = 16000

# sherpa-onnx backend: pyannote segmentation-3.0 + a WeSpeaker ResNet34 embedding (the
# embedding pyannote 3.1 itself uses), both ONNX exports published by k2-fsa. No HF token.
_RELEASES = "https://github.com/k2-fsa/sherpa-onnx/releases/download"
SEGMENTATION_NAME = "sherpa-onnx-pyannote-segmentation-3-0"
SEGMENTATION_URL = f"{_RELEASES}/speaker-segmentation-models/{SEGMENTATION_NAME}.tar.bz2"
EMBEDDING_NAME = "wespeaker_en_voxceleb_resnet34_LM.onnx"
EMBEDDING_URL = f"{_RELEASES}/speaker-recongition-models/{EMBEDDING_NAME}"  # sic: the release tag is misspelt
# Agglomerative clustering distance used when the speaker count is unknown; lower -> more speakers.
CLUSTER_THRESHOLD = 0.5

PYANNOTE_PIPELINE = "pyannote/speaker-diarization-3.1"


def diarize(
    audio_path: str,
    num_speakers: int | None = None,
    min_speakers: int | None = None,
    max_speakers: int | None = None,
    backend: str = "sherpa",
    progress: ProgressFn | None = None,
) -> list[SpeakerTurn]:
    """Return speaker turns sorted by start, labelled SPEAKER_00, SPEAKER_01, ... in order of first appearance."""
    if min_speakers is not None and max_speakers is not None and min_speakers > max_speakers:
        raise ValueError(f"min_speakers ({min_speakers}) > max_speakers ({max_speakers})")
    if num_speakers is not None:
        if num_speakers < 1:
            raise ValueError(f"num_speakers must be >= 1, got {num_speakers}")
        num_speakers = _clamp(num_speakers, min_speakers, max_speakers)
    elif min_speakers is not None and min_speakers == max_speakers:
        num_speakers = min_speakers

    progress = _monotonic(progress)
    if backend == "sherpa":
        raw = _diarize_sherpa(audio_path, num_speakers, min_speakers, max_speakers, progress)
    elif backend == "pyannote":
        raw = _diarize_pyannote(audio_path, num_speakers, min_speakers, max_speakers, progress)
    else:
        raise ValueError(f"unknown diarization backend {backend!r}; expected 'sherpa' or 'pyannote'")
    return relabel(raw)


def relabel(raw: list[tuple[float, float, Hashable]]) -> list[SpeakerTurn]:
    """(start, end, backend label) -> SpeakerTurns sorted by start, labels SPEAKER_NN by first appearance."""
    labels: dict[Hashable, str] = {}
    turns = []
    for start, end, label in sorted(raw, key=lambda r: (r[0], r[1])):
        if end <= start:
            continue
        speaker = labels.setdefault(label, f"SPEAKER_{len(labels):02d}")
        turns.append(SpeakerTurn(speaker=speaker, start=float(start), end=float(end)))
    return turns


def _clamp(n: int, lo: int | None, hi: int | None) -> int:
    if lo is not None:
        n = max(n, lo)
    if hi is not None:
        n = min(n, hi)
    return n


def _monotonic(progress: ProgressFn | None) -> ProgressFn:
    """Wrap progress so it never goes backwards (e.g. when clustering is re-run)."""
    last = 0.0

    def report(frac: float) -> None:
        nonlocal last
        frac = min(max(frac, last), 1.0)
        if progress and frac > last:
            progress(frac)
        last = frac

    return report


# --- audio -------------------------------------------------------------------


def load_audio(path: str, sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    """Read audio as mono float32 at sample_rate. WAV/FLAC/OGG need only soundfile + numpy."""
    import soundfile as sf

    try:
        data, sr = sf.read(path, dtype="float32", always_2d=True)
    except RuntimeError:  # soundfile.LibsndfileError: a format libsndfile can't read (mp3/aac/...)
        from faster_whisper import decode_audio  # PyAV, bundled with faster-whisper

        return decode_audio(path, sampling_rate=sample_rate)
    return resample(data.mean(axis=1), sr, sample_rate)


def resample(x: np.ndarray, sr_in: int, sr_out: int, zeros: int = 8, chunk: int = 1 << 15) -> np.ndarray:
    """Band-limited (Hann-windowed sinc) resampling of a 1-D signal."""
    x = np.asarray(x, dtype=np.float32)
    if sr_in == sr_out:
        return x
    g = math.gcd(sr_in, sr_out)
    up, down = sr_out // g, sr_in // g  # output sample n sits at input position n * down / up
    cutoff = min(1.0, up / down)  # anti-aliasing cut-off, as a fraction of the input Nyquist
    half = int(np.ceil(zeros / cutoff))
    offsets = np.arange(-half + 1, half + 1)
    # Polyphase: the fractional position only takes `up` values, so tabulate one kernel per phase.
    dist = (np.arange(up) / up)[:, None] - offsets[None, :]
    kernels = (cutoff * np.sinc(cutoff * dist) * (0.5 + 0.5 * np.cos(np.pi * dist / half))).astype(np.float32)
    padded = np.pad(x, (half, half + 1))
    n_out = len(x) * up // down
    out = np.empty(n_out, dtype=np.float32)
    for c0 in range(0, n_out, chunk):
        num = np.arange(c0, min(c0 + chunk, n_out), dtype=np.int64) * down
        base, phase = num // up, num % up
        out[c0 : c0 + len(num)] = np.einsum(
            "ij,ij->i", padded[base[:, None] + (offsets + half)[None, :]], kernels[phase]
        )
    return out


# --- sherpa-onnx backend -----------------------------------------------------


def sherpa_models(cache_dir: str | Path | None = None) -> tuple[Path, Path]:
    """Paths of the (segmentation, embedding) ONNX models, downloading them on first use."""
    root = Path(cache_dir) if cache_dir else models_dir() / "sherpa-onnx"
    segmentation = root / SEGMENTATION_NAME / "model.onnx"
    if not segmentation.exists():
        archive = root / f"{SEGMENTATION_NAME}.tar.bz2"
        _download(SEGMENTATION_URL, archive)
        try:
            _extract_member(archive, "model.onnx", segmentation)
        finally:
            archive.unlink(missing_ok=True)
    embedding = root / EMBEDDING_NAME
    if not embedding.exists():
        _download(EMBEDDING_URL, embedding)
    return segmentation, embedding


def _download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    try:
        with urllib.request.urlopen(url, timeout=60) as resp, open(tmp, "wb") as f:
            shutil.copyfileobj(resp, f, 1 << 20)
        os.replace(tmp, dest)
    except Exception as e:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(
            f"could not download {url} to {dest}: {e} (download it there by hand to work offline)"
        ) from e


def _extract_member(archive: Path, name: str, dest: Path) -> None:
    """Copy the archive's file called `name` (at any depth) to dest; nothing else is extracted."""
    with tarfile.open(archive, "r:*") as tar:
        member = next((m for m in tar.getmembers() if m.isfile() and PurePosixPath(m.name).name == name), None)
        if member is None:
            raise RuntimeError(f"{name} not found in {archive}")
        src = tar.extractfile(member)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        with src, open(tmp, "wb") as f:
            shutil.copyfileobj(src, f, 1 << 20)
        os.replace(tmp, dest)


def _diarize_sherpa(audio_path, num_speakers, min_speakers, max_speakers, progress):
    import sherpa_onnx

    segmentation, embedding = sherpa_models()
    threads = max(1, min(os.cpu_count() or 1, 8))

    def build(num_clusters: int | None):
        config = sherpa_onnx.OfflineSpeakerDiarizationConfig(
            segmentation=sherpa_onnx.OfflineSpeakerSegmentationModelConfig(
                pyannote=sherpa_onnx.OfflineSpeakerSegmentationPyannoteModelConfig(model=str(segmentation)),
                num_threads=threads,
            ),
            embedding=sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(embedding), num_threads=threads),
            clustering=sherpa_onnx.FastClusteringConfig(
                num_clusters=num_clusters if num_clusters else -1, threshold=CLUSTER_THRESHOLD
            ),
            min_duration_on=0.3,
            min_duration_off=0.5,
        )
        if not config.validate():
            raise RuntimeError(f"invalid sherpa-onnx diarization config (models: {segmentation}, {embedding})")
        return sherpa_onnx.OfflineSpeakerDiarization(config)

    sd = build(num_speakers)
    audio = load_audio(audio_path, sd.sample_rate)

    def run(sd) -> list[tuple[float, float, int]]:
        def callback(done: int, total: int) -> int:
            if total:
                progress(done / total)
            return 0  # 0 = keep going

        return [(s.start, s.end, s.speaker) for s in sd.process(audio, callback=callback).sort_by_start_time()]

    raw = run(sd)
    if num_speakers is None:
        found = len({speaker for _, _, speaker in raw})
        wanted = _clamp(found, min_speakers, max_speakers)
        if raw and wanted != found:  # threshold clustering landed outside [min, max]: force the count
            raw = run(build(wanted))
    return raw


# --- pyannote backend (optional) ---------------------------------------------


def _diarize_pyannote(audio_path, num_speakers, min_speakers, max_speakers, progress):
    try:
        import torch
        from pyannote.audio import Pipeline
    except ImportError as e:
        raise RuntimeError("the pyannote backend needs pyannote.audio: pip install pyannote.audio") from e
    token = os.environ.get("HF_TOKEN")
    if not token:
        raise RuntimeError(
            f"the pyannote backend needs a Hugging Face token in HF_TOKEN (with {PYANNOTE_PIPELINE} terms accepted)"
        )
    try:
        pipeline = Pipeline.from_pretrained(PYANNOTE_PIPELINE, token=token)
    except TypeError:  # pyannote.audio < 4
        pipeline = Pipeline.from_pretrained(PYANNOTE_PIPELINE, use_auth_token=token)
    if pipeline is None:
        raise RuntimeError(f"could not load {PYANNOTE_PIPELINE}; accept its terms on Hugging Face for this token")
    if torch.cuda.is_available():
        pipeline.to(torch.device("cuda"))

    if num_speakers is not None:
        counts = {"num_speakers": num_speakers}
    else:
        counts = {k: v for k, v in (("min_speakers", min_speakers), ("max_speakers", max_speakers)) if v is not None}
    audio = load_audio(audio_path, SAMPLE_RATE)
    waveform = {"waveform": torch.from_numpy(audio).unsqueeze(0), "sample_rate": SAMPLE_RATE}
    output = pipeline(waveform, hook=_pyannote_hook(progress), **counts)
    annotation = getattr(output, "speaker_diarization", output)  # pyannote.audio 4 wraps the Annotation
    return [(turn.start, turn.end, label) for turn, _, label in annotation.itertracks(yield_label=True)]


# Share of the run each pyannote step with per-chunk progress covers.
_PYANNOTE_STEPS = {"segmentation": (0.0, 0.3), "embeddings": (0.3, 0.95)}


def _pyannote_hook(progress: ProgressFn):
    def hook(step_name, step_artifact, file=None, total=None, completed=None):
        if step_name in _PYANNOTE_STEPS and total:
            lo, hi = _PYANNOTE_STEPS[step_name]
            progress(lo + (hi - lo) * (completed or 0) / total)

    return hook
