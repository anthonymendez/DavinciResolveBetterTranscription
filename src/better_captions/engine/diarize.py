from __future__ import annotations

import math
import os
import shutil
import sys
import tarfile
import time
from collections.abc import Callable, Hashable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

import numpy as np

from better_captions.engine.download import download
from better_captions.engine.merge_speakers import MINOR_SECONDS, MINOR_SHARE, EmbedFn, merge_minor_speakers
from better_captions.engine.reassign_segments import TurnScore, reassign_segments
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


@dataclass
class DiarizeResult:
    """diarize()'s turns plus what the reassignment pass learned about the voices (empty without embeddings)."""

    turns: list[SpeakerTurn]
    centroids: dict[str, np.ndarray] = field(default_factory=dict)  # speaker -> unit-length voice centroid
    scores: list[TurnScore | None] = field(default_factory=list)  # aligned with turns; label is a speaker name
    # the embedding model the centroids come from (voices from another model are not comparable); None without
    embedding_model: str | None = None


def diarize(
    audio_path: str,
    num_speakers: int | None = None,
    min_speakers: int | None = None,
    max_speakers: int | None = None,
    backend: str = "sherpa",
    progress: ProgressFn | None = None,
    log: LogFn | None = None,
    merge_minor: bool = True,
    minor_seconds: float = MINOR_SECONDS,
    minor_share: float = MINOR_SHARE,
    reassign: bool = True,
) -> list[SpeakerTurn]:
    """Return speaker turns sorted by start, labelled SPEAKER_00, SPEAKER_01, ... in order of first appearance.

    Status lines (started, models loading, progress, speakers found, finished) go to `log`; by default
    that is stderr, which the Resolve plugin shows in its log window (also without --verbose).

    With merge_minor, clusters with little speech (under max(minor_seconds, minor_share of all speech), see
    merge_speakers) are folded into the most similar remaining speaker. The result never drops below
    num_speakers (or min_speakers), so a forced count that clustering already met is left alone.

    With reassign, each turn is then re-checked against every speaker's voice and moved when another speaker
    matches it clearly better (see reassign_segments). Use diarize_detailed() to also get the voice centroids
    and per-turn similarities.
    """
    return diarize_detailed(
        audio_path,
        num_speakers,
        min_speakers,
        max_speakers,
        backend,
        progress,
        log,
        merge_minor,
        minor_seconds,
        minor_share,
        reassign,
    ).turns


def diarize_detailed(
    audio_path: str,
    num_speakers: int | None = None,
    min_speakers: int | None = None,
    max_speakers: int | None = None,
    backend: str = "sherpa",
    progress: ProgressFn | None = None,
    log: LogFn | None = None,
    merge_minor: bool = True,
    minor_seconds: float = MINOR_SECONDS,
    minor_share: float = MINOR_SHARE,
    reassign: bool = True,
) -> DiarizeResult:
    """diarize(), returning the turns together with the speakers' voice centroids and per-turn similarities."""
    if min_speakers is not None and max_speakers is not None and min_speakers > max_speakers:
        raise ValueError(f"min_speakers ({min_speakers}) > max_speakers ({max_speakers})")
    if num_speakers is not None:
        if num_speakers < 1:
            raise ValueError(f"num_speakers must be >= 1, got {num_speakers}")
        num_speakers = _clamp(num_speakers, min_speakers, max_speakers)
    elif min_speakers is not None and min_speakers == max_speakers:
        num_speakers = min_speakers

    if backend not in ("sherpa", "pyannote"):
        raise ValueError(f"unknown diarization backend {backend!r}; expected 'sherpa' or 'pyannote'")

    status = _Status(log or _stderr_log)
    status.started(backend)
    progress = _monotonic(progress, status)
    if backend == "sherpa":
        raw, embed = _diarize_sherpa(audio_path, num_speakers, min_speakers, max_speakers, progress, status)
    else:  # pyannote: no embedder of our own, so minor clusters merge by time
        raw, embed = _diarize_pyannote(audio_path, num_speakers, min_speakers, max_speakers, progress, status), None
    if merge_minor:
        merged = merge_minor_speakers(
            raw,
            embed,
            min_seconds=minor_seconds,
            min_share=minor_share,
            keep_at_least=num_speakers if num_speakers is not None else (min_speakers or 1),
        )
        if merged.minor:
            status.merged(merged.minor, merged.remaining)
        raw = merged.raw
    moved = None
    if reassign and embed is not None:
        moved = reassign_segments(raw, embed)
        if moved.moved:
            status.reassigned(moved.moved)
        raw = moved.raw
    turns, order, names = _relabel(raw)
    status.finished(len({t.speaker for t in turns}))
    if moved is None:
        return DiarizeResult(turns, {}, [None] * len(turns))
    centroids = {names[lb]: c for lb, c in moved.centroids.items() if lb in names}
    scores = []
    for i in order:
        score = moved.scores[i]
        if score is not None and score.label in names:
            score = TurnScore(names[score.label], score.best, score.second)
        scores.append(score)
    return DiarizeResult(turns, centroids, scores, EMBEDDING_NAME if centroids else None)


def relabel(raw: list[tuple[float, float, Hashable]]) -> list[SpeakerTurn]:
    """(start, end, backend label) -> SpeakerTurns sorted by start, labels SPEAKER_NN by first appearance."""
    return _relabel(raw)[0]


def _relabel(raw: list[tuple[float, float, Hashable]]) -> tuple[list[SpeakerTurn], list[int], dict[Hashable, str]]:
    """relabel(), plus the index in `raw` of each returned turn and the backend label -> speaker name map."""
    names: dict[Hashable, str] = {}
    turns = []
    order = []
    for i in sorted(range(len(raw)), key=lambda i: (raw[i][0], raw[i][1])):
        start, end, label = raw[i]
        if end <= start:
            continue
        speaker = names.setdefault(label, f"SPEAKER_{len(names):02d}")
        turns.append(SpeakerTurn(speaker=speaker, start=float(start), end=float(end)))
        order.append(i)
    return turns, order, names


def _clamp(n: int, lo: int | None, hi: int | None) -> int:
    if lo is not None:
        n = max(n, lo)
    if hi is not None:
        n = min(n, hi)
    return n


def _monotonic(progress: ProgressFn | None, status: _Status | None = None) -> ProgressFn:
    """Wrap progress so it never goes backwards (e.g. when clustering is re-run)."""
    last = 0.0

    def report(frac: float) -> None:
        nonlocal last
        frac = min(max(frac, last), 1.0)
        if frac > last:
            if progress:
                progress(frac)
            if status:
                status.progress(frac)
        last = frac

    return report


# --- status reporting --------------------------------------------------------

LogFn = Callable[[str], None]

# Progress is logged each time it crosses another multiple of this share of the audio.
PROGRESS_LOG_STEP = 0.1


def _stderr_log(message: str) -> None:
    """The engine's stderr is what the plugin's runner turns into `log` events (and so its log window)."""
    try:
        print(message, file=sys.stderr, flush=True)
    except (OSError, ValueError):  # stderr closed or detached: status lines are never worth failing a run
        pass


def format_clock(seconds: float) -> str:
    """12:05 / 1:02:03: a position in the audio."""
    s = int(round(max(seconds, 0.0)))
    h, rest = divmod(s, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def format_elapsed(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    m, s = divmod(int(round(seconds)), 60)
    return f"{m}m {s:02d}s"


class _Status:
    """Throttled status lines for one diarization run."""

    def __init__(self, log: LogFn):
        self._log = log
        self._t0 = time.monotonic()
        self._duration: float | None = None  # seconds of audio, once loaded
        self._bucket = 0

    def started(self, backend: str) -> None:
        self._log(f"Detecting speakers: started ({backend} backend)")

    def loading_models(self) -> None:
        self._log("Detecting speakers: loading models")

    def audio_loaded(self, seconds: float) -> None:
        self._duration = seconds
        self._log(f"Detecting speakers: analysing {format_clock(seconds)} of audio")

    def progress(self, frac: float) -> None:
        bucket = int(frac / PROGRESS_LOG_STEP + 1e-9)
        if bucket <= self._bucket:
            return
        self._bucket = bucket
        text = f"Detecting speakers: {int(frac * 100 + 1e-9)}%"
        if self._duration:
            text += f" ({format_clock(frac * self._duration)} of {format_clock(self._duration)})"
        self._log(text)

    def retrying(self, speakers: int) -> None:
        self._log(f"Detecting speakers: re-clustering for {speakers} speaker{'s' if speakers != 1 else ''}")

    def merged(self, minor: int, remaining: int) -> None:
        self._log(f"Detecting speakers: merged {minor} minor speaker{'s' if minor != 1 else ''} into {remaining}")

    def reassigned(self, moved: int) -> None:
        self._log(f"Detecting speakers: moved {moved} turn{'s' if moved != 1 else ''} to a better-matching speaker")

    def no_embeddings(self, error: BaseException) -> None:
        self._log(f"Detecting speakers: voice embeddings unavailable ({error}); merging by time instead")

    def finished(self, speakers: int) -> None:
        self._log(f"Detecting speakers: found {speakers} speaker{'s' if speakers != 1 else ''}")
        self._log(f"Detecting speakers: finished in {format_elapsed(time.monotonic() - self._t0)}")


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
    """download(), but a failure to move the finished file into place is reported as a download error too."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        download(url, dest)
    except OSError as e:  # download() wraps fetch errors in RuntimeError; this is its final os.replace
        dest.with_name(dest.name + ".part").unlink(missing_ok=True)
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


def _diarize_sherpa(audio_path, num_speakers, min_speakers, max_speakers, progress, status):
    import sherpa_onnx

    status.loading_models()
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
    status.audio_loaded(len(audio) / sd.sample_rate)

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
            status.retrying(wanted)
            raw = run(build(wanted))
    return raw, _sherpa_embedder(embedding, threads, audio, sd.sample_rate, status)


# Turns shorter than this give unreliable speaker embeddings; they are matched some other way.
MIN_EMBED_SECONDS = 0.5


def _sherpa_embedder(model: Path, threads: int, audio: np.ndarray, sample_rate: int, status: _Status) -> EmbedFn:
    """(start, end) -> the WeSpeaker embedding of that stretch of `audio`, with the model diarization used.

    The extractor is only loaded when the first embedding is asked for (i.e. there are minor speakers to
    merge). If it can't be loaded or run, that is reported once and every later call returns None, so the
    merge falls back to timing instead of failing the run.
    """
    extractor = None
    failed = False

    def embed(start: float, end: float) -> np.ndarray | None:
        nonlocal extractor, failed
        a, b = max(0, int(start * sample_rate)), min(len(audio), int(end * sample_rate))
        if failed or b - a < MIN_EMBED_SECONDS * sample_rate:
            return None
        try:
            if extractor is None:
                import sherpa_onnx

                config = sherpa_onnx.SpeakerEmbeddingExtractorConfig(model=str(model), num_threads=threads)
                extractor = sherpa_onnx.SpeakerEmbeddingExtractor(config)
            stream = extractor.create_stream()
            stream.accept_waveform(sample_rate=sample_rate, waveform=audio[a:b])
            stream.input_finished()
            if not extractor.is_ready(stream):
                return None
            return np.asarray(extractor.compute(stream), dtype=np.float32)
        except Exception as e:  # an optional refinement: never worth failing the run over
            failed = True
            status.no_embeddings(e)
            return None

    return embed


# --- pyannote backend (optional) ---------------------------------------------


def _diarize_pyannote(audio_path, num_speakers, min_speakers, max_speakers, progress, status):
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
    status.loading_models()
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
    status.audio_loaded(len(audio) / SAMPLE_RATE)
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
