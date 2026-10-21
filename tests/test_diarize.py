"""diarize() with sherpa-onnx / pyannote replaced by fakes; downloads are faked too."""

import io
import sys
import tarfile
import types
from pathlib import Path
from types import SimpleNamespace as NS

import numpy as np
import pytest
import soundfile as sf

from better_captions import paths
from better_captions.engine import diarize as dz
from better_captions.schema import SpeakerTurn

# --- data dir / model cache --------------------------------------------------


def test_data_dir_override(monkeypatch, tmp_path):
    monkeypatch.setenv("BETTER_CAPTIONS_HOME", str(tmp_path / "bc"))
    assert paths.data_dir() == tmp_path / "bc"
    assert paths.models_dir() == tmp_path / "bc" / "models"


@pytest.mark.parametrize(
    "platform,env,expected",
    [
        ("win32", {"LOCALAPPDATA": "/lad"}, "/lad/BetterCaptions"),
        ("win32", {}, "/home/u/AppData/Local/BetterCaptions"),
        ("darwin", {}, "/home/u/Library/Application Support/BetterCaptions"),
        ("linux", {"XDG_DATA_HOME": "/xdg"}, "/xdg/BetterCaptions"),
        ("linux", {}, "/home/u/.local/share/BetterCaptions"),
    ],
)
def test_platform_data_dir(monkeypatch, platform, env, expected):
    for var in ("BETTER_CAPTIONS_HOME", "LOCALAPPDATA", "XDG_DATA_HOME"):
        monkeypatch.delenv(var, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(paths.sys, "platform", platform)
    monkeypatch.setattr(paths.Path, "home", classmethod(lambda cls: Path("/home/u")))
    assert paths.data_dir() == Path(expected)


def _segmentation_tarball() -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:bz2") as tar:
        for name, data in [
            ("sherpa-onnx-pyannote-segmentation-3-0/README.md", b"readme"),
            ("sherpa-onnx-pyannote-segmentation-3-0/model.onnx", b"SEGMENTATION"),
            ("../escape.txt", b"nope"),
        ]:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.fixture
def fake_downloads(monkeypatch):
    """Serve the model URLs from memory; record what was fetched."""
    fetched = []
    payloads = {dz.SEGMENTATION_URL: _segmentation_tarball(), dz.EMBEDDING_URL: b"EMBEDDING"}

    def urlopen(url, timeout=None):
        fetched.append(url)
        if url not in payloads:
            raise OSError(f"unexpected url {url}")
        return io.BytesIO(payloads[url])

    monkeypatch.setattr(dz.urllib.request, "urlopen", urlopen)
    return NS(fetched=fetched, payloads=payloads)


def test_sherpa_models_download_once_into_data_dir(monkeypatch, tmp_path, fake_downloads):
    monkeypatch.setenv("BETTER_CAPTIONS_HOME", str(tmp_path / "home"))
    seg, emb = dz.sherpa_models()
    cache = tmp_path / "home" / "models" / "sherpa-onnx"
    assert seg == cache / "sherpa-onnx-pyannote-segmentation-3-0" / "model.onnx"
    assert emb == cache / "wespeaker_en_voxceleb_resnet34_LM.onnx"
    assert seg.read_bytes() == b"SEGMENTATION"
    assert emb.read_bytes() == b"EMBEDDING"
    assert fake_downloads.fetched == [dz.SEGMENTATION_URL, dz.EMBEDDING_URL]
    # Only model.onnx is extracted; the archive and partial files are gone.
    assert sorted(p.relative_to(cache).as_posix() for p in cache.rglob("*")) == [
        "sherpa-onnx-pyannote-segmentation-3-0",
        "sherpa-onnx-pyannote-segmentation-3-0/model.onnx",
        "wespeaker_en_voxceleb_resnet34_LM.onnx",
    ]
    assert not (tmp_path / "escape.txt").exists() and not (tmp_path / "home" / "models" / "escape.txt").exists()

    assert dz.sherpa_models() == (seg, emb)
    assert len(fake_downloads.fetched) == 2  # cached: nothing fetched again


def test_sherpa_models_only_fetch_whats_missing(tmp_path, fake_downloads):
    (tmp_path / "wespeaker_en_voxceleb_resnet34_LM.onnx").write_bytes(b"mine")
    seg, emb = dz.sherpa_models(tmp_path)
    assert fake_downloads.fetched == [dz.SEGMENTATION_URL]
    assert emb.read_bytes() == b"mine"


def test_download_failure_leaves_no_partial_file(tmp_path, fake_downloads):
    del fake_downloads.payloads[dz.EMBEDDING_URL]
    with pytest.raises(RuntimeError, match="could not download"):
        dz.sherpa_models(tmp_path)
    assert not (tmp_path / "wespeaker_en_voxceleb_resnet34_LM.onnx").exists()
    assert not list(tmp_path.glob("*.part"))


def test_archive_without_model_is_an_error(tmp_path, fake_downloads):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:bz2"):
        pass
    fake_downloads.payloads[dz.SEGMENTATION_URL] = buf.getvalue()
    with pytest.raises(RuntimeError, match="model.onnx not found"):
        dz.sherpa_models(tmp_path)
    assert not list(tmp_path.glob("*.tar.bz2"))


# --- audio -------------------------------------------------------------------


def _sine(freq, sr, seconds=1.0):
    t = np.arange(int(sr * seconds)) / sr
    return np.sin(2 * np.pi * freq * t).astype(np.float32)


def test_load_audio_downmixes_and_resamples_wav(tmp_path):
    left, right = _sine(440, 48000), 0.5 * _sine(440, 48000)
    path = tmp_path / "stereo48k.wav"
    sf.write(path, np.stack([left, right], axis=1), 48000)
    audio = dz.load_audio(str(path))
    assert audio.dtype == np.float32 and audio.ndim == 1 and len(audio) == 16000
    expected = 0.75 * _sine(440, 16000)
    assert np.max(np.abs(audio[200:-200] - expected[200:-200])) < 0.01


def test_load_audio_16k_mono_passthrough(tmp_path):
    x = _sine(300, 16000) * 0.5
    path = tmp_path / "mono16k.wav"
    sf.write(path, x, 16000, subtype="FLOAT")
    np.testing.assert_allclose(dz.load_audio(str(path)), x, atol=1e-6)


def test_load_audio_falls_back_to_pyav_for_other_formats(tmp_path, monkeypatch):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"not audio libsndfile understands")
    calls = []

    def decode_audio(p, sampling_rate):
        calls.append((p, sampling_rate))
        return np.zeros(3, dtype=np.float32)

    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(decode_audio=decode_audio))
    assert len(dz.load_audio(str(path))) == 3
    assert calls == [(str(path), 16000)]


def test_resample_suppresses_aliasing():
    # 12 kHz is above the 8 kHz Nyquist of 16 kHz audio: it must be filtered out, not folded down to 4 kHz.
    out = dz.resample(_sine(12000, 48000), 48000, 16000)
    assert np.max(np.abs(out[200:-200])) < 0.02


def test_resample_non_integer_ratio():
    out = dz.resample(_sine(1000, 44100), 44100, 16000)
    assert len(out) == 16000
    assert np.max(np.abs(out[200:-200] - _sine(1000, 16000)[200:-200])) < 0.01


def test_resample_upsamples():
    out = dz.resample(_sine(440, 8000), 8000, 16000)
    assert len(out) == 16000
    assert np.max(np.abs(out[200:-200] - _sine(440, 16000)[200:-200])) < 0.01


# --- relabelling / argument handling -------------------------------------------


def test_relabel_by_first_appearance():
    raw = [(3.0, 4.0, 0), (0.0, 1.0, 2), (1.0, 2.5, 0), (5.0, 6.0, 1), (7.0, 7.0, 2)]
    assert dz.relabel(raw) == [
        SpeakerTurn("SPEAKER_00", 0.0, 1.0),
        SpeakerTurn("SPEAKER_01", 1.0, 2.5),
        SpeakerTurn("SPEAKER_01", 3.0, 4.0),
        SpeakerTurn("SPEAKER_02", 5.0, 6.0),
    ]


def test_bad_arguments():
    with pytest.raises(ValueError, match="backend"):
        dz.diarize("x.wav", backend="nope")
    with pytest.raises(ValueError, match="min_speakers"):
        dz.diarize("x.wav", min_speakers=3, max_speakers=2)
    with pytest.raises(ValueError, match="num_speakers"):
        dz.diarize("x.wav", num_speakers=0)


# --- sherpa backend ------------------------------------------------------------


class _Config:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

    def validate(self):
        return True


def make_fake_sherpa(results):
    """results: num_clusters -> [(start, end, int speaker)] the fake diarizer returns."""
    runs = []

    class OfflineSpeakerDiarization:
        sample_rate = 16000

        def __init__(self, config):
            self.config = config

        def process(self, samples, callback=None):
            num_clusters = self.config.clustering.num_clusters
            runs.append(NS(config=self.config, samples=samples, num_clusters=num_clusters))
            for i in range(1, 5):
                assert callback(i, 4) == 0
            segments = [NS(start=s, end=e, speaker=k) for s, e, k in results[num_clusters]]
            return NS(sort_by_start_time=lambda: sorted(segments, key=lambda s: s.start))

    mod = types.SimpleNamespace(
        OfflineSpeakerDiarizationConfig=_Config,
        OfflineSpeakerSegmentationModelConfig=_Config,
        OfflineSpeakerSegmentationPyannoteModelConfig=_Config,
        SpeakerEmbeddingExtractorConfig=_Config,
        FastClusteringConfig=_Config,
        OfflineSpeakerDiarization=OfflineSpeakerDiarization,
    )
    return mod, runs


THREE = [(4.0, 6.0, 0), (0.0, 2.0, 2), (2.0, 4.0, 1), (6.0, 7.0, 2)]
TWO = [(4.0, 6.0, 1), (0.0, 4.0, 0), (6.0, 7.0, 0)]
ONE = [(0.0, 7.0, 0)]
FOUR = THREE + [(7.0, 8.0, 3)]


@pytest.fixture
def sherpa_env(monkeypatch, tmp_path, fake_downloads):
    monkeypatch.setenv("BETTER_CAPTIONS_HOME", str(tmp_path / "home"))
    mod, runs = make_fake_sherpa({-1: THREE, 1: ONE, 2: TWO, 4: FOUR})
    monkeypatch.setitem(sys.modules, "sherpa_onnx", mod)
    audio = tmp_path / "in.wav"
    sf.write(audio, np.zeros((48000 * 2, 2), dtype=np.float32), 48000)
    return NS(runs=runs, audio=str(audio), downloads=fake_downloads, home=tmp_path / "home")


def test_sherpa_threshold_clustering_and_relabel(sherpa_env):
    seen = []
    turns = dz.diarize(sherpa_env.audio, progress=seen.append)
    assert turns == [
        SpeakerTurn("SPEAKER_00", 0.0, 2.0),
        SpeakerTurn("SPEAKER_01", 2.0, 4.0),
        SpeakerTurn("SPEAKER_02", 4.0, 6.0),
        SpeakerTurn("SPEAKER_00", 6.0, 7.0),
    ]
    assert seen == [0.25, 0.5, 0.75, 1.0]
    (run,) = sherpa_env.runs
    clustering = run.config.clustering
    assert (clustering.num_clusters, clustering.threshold) == (-1, dz.CLUSTER_THRESHOLD)
    # 2 s of 48 kHz stereo arrives as 16 kHz mono.
    assert run.samples.shape == (32000,) and run.samples.dtype == np.float32
    # Models were fetched into the cache and handed to sherpa.
    models = sherpa_env.home / "models" / "sherpa-onnx"
    assert run.config.segmentation.pyannote.model == str(models / dz.SEGMENTATION_NAME / "model.onnx")
    assert run.config.embedding.model == str(models / dz.EMBEDDING_NAME)
    assert sherpa_env.downloads.fetched == [dz.SEGMENTATION_URL, dz.EMBEDDING_URL]


@pytest.mark.parametrize(
    "kwargs,clusters,count",
    [
        ({"num_speakers": 2}, [2], 2),
        ({"num_speakers": 5, "max_speakers": 4}, [4], 4),
        ({"num_speakers": 1, "min_speakers": 2}, [2], 2),
        ({"min_speakers": 2, "max_speakers": 2}, [2], 2),
        ({"max_speakers": 2}, [-1, 2], 2),  # threshold found 3: re-run forcing 2
        ({"min_speakers": 4}, [-1, 4], 4),  # threshold found 3: re-run forcing 4
        ({"min_speakers": 1, "max_speakers": 3}, [-1], 3),  # within range: single run
    ],
)
def test_sherpa_speaker_counts(sherpa_env, kwargs, clusters, count):
    seen = []
    turns = dz.diarize(sherpa_env.audio, progress=seen.append, **kwargs)
    assert [r.num_clusters for r in sherpa_env.runs] == clusters
    assert len({t.speaker for t in turns}) == count
    assert seen == sorted(seen) and seen[-1] == 1.0  # never goes backwards across re-runs


# --- pyannote backend --------------------------------------------------------


class FakeAnnotation:
    def __init__(self, tracks):
        self.tracks = tracks

    def itertracks(self, yield_label=False):
        for s, e, label in self.tracks:
            yield NS(start=s, end=e), None, label


def make_fake_pyannote(monkeypatch, wrap_output=False, legacy=False):
    calls = NS(from_pretrained=None, call=None)

    class Pipeline:
        @classmethod
        def from_pretrained(cls, name, **kwargs):
            if legacy and "token" in kwargs:
                raise TypeError("unexpected keyword argument 'token'")
            calls.from_pretrained = (name, kwargs)
            return cls()

        def to(self, device):
            calls.device = device

        def __call__(self, audio, hook=None, **kwargs):
            calls.call = (audio, kwargs)
            for i in range(1, 3):
                hook("segmentation", None, total=2, completed=i)
                hook("embeddings", None, total=2, completed=i)
            hook("discrete_diarization", None)
            ann = FakeAnnotation([(2.0, 3.0, "B"), (0.0, 1.5, "A"), (3.0, 4.0, "A")])
            return NS(speaker_diarization=ann) if wrap_output else ann

    class Tensor:
        def __init__(self, array):
            self.array = array

        def unsqueeze(self, dim):
            return Tensor(np.expand_dims(self.array, dim))

    torch = types.SimpleNamespace(
        from_numpy=Tensor,
        cuda=types.SimpleNamespace(is_available=lambda: False),
        device=lambda name: name,
    )
    monkeypatch.setitem(sys.modules, "torch", torch)
    monkeypatch.setitem(sys.modules, "pyannote", types.ModuleType("pyannote"))
    monkeypatch.setitem(sys.modules, "pyannote.audio", types.SimpleNamespace(Pipeline=Pipeline))
    return calls


@pytest.mark.parametrize("wrap_output,legacy", [(False, False), (True, False), (False, True)])
def test_pyannote_backend(monkeypatch, tmp_path, wrap_output, legacy):
    calls = make_fake_pyannote(monkeypatch, wrap_output=wrap_output, legacy=legacy)
    monkeypatch.setenv("HF_TOKEN", "hf_x")
    audio = tmp_path / "in.wav"
    sf.write(audio, np.zeros(16000, dtype=np.float32), 16000)
    seen = []
    turns = dz.diarize(str(audio), num_speakers=2, max_speakers=3, backend="pyannote", progress=seen.append)
    assert turns == [
        SpeakerTurn("SPEAKER_00", 0.0, 1.5),
        SpeakerTurn("SPEAKER_01", 2.0, 3.0),
        SpeakerTurn("SPEAKER_00", 3.0, 4.0),
    ]
    name, kwargs = calls.from_pretrained
    assert name == dz.PYANNOTE_PIPELINE
    assert kwargs == ({"use_auth_token": "hf_x"} if legacy else {"token": "hf_x"})
    waveform, counts = calls.call
    assert counts == {"num_speakers": 2}
    assert waveform["sample_rate"] == 16000 and waveform["waveform"].array.shape == (1, 16000)
    assert seen == sorted(seen) and seen[-1] == pytest.approx(0.95)


def test_pyannote_min_max_passed_through(monkeypatch, tmp_path):
    calls = make_fake_pyannote(monkeypatch)
    monkeypatch.setenv("HF_TOKEN", "hf_x")
    audio = tmp_path / "in.wav"
    sf.write(audio, np.zeros(1600, dtype=np.float32), 16000)
    dz.diarize(str(audio), min_speakers=2, max_speakers=4, backend="pyannote")
    assert calls.call[1] == {"min_speakers": 2, "max_speakers": 4}


def test_pyannote_needs_token(monkeypatch):
    make_fake_pyannote(monkeypatch)
    monkeypatch.delenv("HF_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="HF_TOKEN"):
        dz.diarize("x.wav", backend="pyannote")


def test_pyannote_not_installed(monkeypatch):
    monkeypatch.setitem(sys.modules, "pyannote.audio", None)
    with pytest.raises(RuntimeError, match="pip install pyannote.audio"):
        dz.diarize("x.wav", backend="pyannote")
