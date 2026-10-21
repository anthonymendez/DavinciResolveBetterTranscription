"""transcribe() with faster-whisper replaced by a fake module (no model download)."""

import subprocess
import sys
import types
from types import SimpleNamespace as NS

import pytest

from better_captions.engine import gpu
from better_captions.engine import transcribe as tr


class FakeWhisperModel:
    instances = []
    fail_on = set()  # devices whose construction raises

    def __init__(self, model, device="auto", device_index=0, compute_type="default"):
        if device in self.fail_on:
            raise RuntimeError(f"{device} unavailable")
        self.args = dict(model=model, device=device, device_index=device_index, compute_type=compute_type)
        self.transcribe_kwargs = None
        FakeWhisperModel.instances.append(self)

    def transcribe(self, audio, **kwargs):
        self.audio, self.transcribe_kwargs = audio, kwargs
        segments = [
            NS(
                end=2.0,
                words=[
                    NS(word=" Hello", start=0.0, end=0.5, probability=0.9),
                    NS(word=" world.", start=0.6, end=2.0, probability=0.8),
                ],
            ),
            NS(
                end=5.0,
                words=[
                    NS(word=" ", start=2.5, end=2.6, probability=0.1),
                    NS(word=" Bye", start=4.0, end=5.0, probability=0.7),
                ],
            ),
            NS(end=10.5, words=None),
        ]
        return (s for s in segments), NS(language="en", duration=10.0)


@pytest.fixture
def fake_whisper(monkeypatch):
    FakeWhisperModel.instances = []
    FakeWhisperModel.fail_on = set()
    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=FakeWhisperModel))
    monkeypatch.setattr(gpu, "setup", lambda: {"dirs": [], "loaded": []})  # never load real CUDA libraries
    monkeypatch.setattr(tr, "_backend_available", lambda name: False)  # "auto" must not pick mlx/whispercpp on the host
    return FakeWhisperModel


def test_transcribe_words_language_duration(fake_whisper, monkeypatch):
    monkeypatch.setattr(tr, "_cuda_available", lambda: False)
    language, duration, words = tr.transcribe("in.wav", model="small", language="en")
    assert (language, duration) == ("en", 10.0)
    assert [(w.text, w.start, w.end, w.probability, w.speaker) for w in words] == [
        ("Hello", 0.0, 0.5, 0.9, None),
        ("world.", 0.6, 2.0, 0.8, None),
        ("Bye", 4.0, 5.0, 0.7, None),
    ]
    model = fake_whisper.instances[0]
    assert model.args["model"] == "small"
    assert model.audio == "in.wav"
    assert model.transcribe_kwargs == {"language": "en", "word_timestamps": True, "vad_filter": True}


def test_progress_from_segment_end_over_duration(fake_whisper, monkeypatch):
    monkeypatch.setattr(tr, "_cuda_available", lambda: False)
    seen = []
    tr.transcribe("in.wav", progress=seen.append)
    assert seen == [0.2, 0.5, 1.0]  # last segment ends past the duration: clamped


@pytest.mark.parametrize(
    "device,cuda,expected",
    [
        ("auto", True, ("cuda", 0, "float16")),
        ("auto", False, ("cpu", 0, "int8")),
        ("cpu", True, ("cpu", 0, "int8")),
        ("cuda", False, ("cuda", 0, "float16")),
        ("cuda:1", False, ("cuda", 1, "float16")),
    ],
)
def test_device_choice(fake_whisper, monkeypatch, device, cuda, expected):
    monkeypatch.setattr(tr, "_cuda_available", lambda: cuda)
    assert tr.resolve_device(device) == expected
    tr.transcribe("in.wav", device=device)
    args = fake_whisper.instances[0].args
    assert (args["device"], args["device_index"], args["compute_type"]) == expected


def test_unknown_device_rejected():
    with pytest.raises(ValueError):
        tr.resolve_device("tpu")


def test_auto_falls_back_to_cpu_when_cuda_fails(fake_whisper, monkeypatch):
    monkeypatch.setattr(tr, "_cuda_available", lambda: True)
    fake_whisper.fail_on = {"cuda"}
    tr.transcribe("in.wav")
    args = fake_whisper.instances[0].args
    assert (args["device"], args["compute_type"]) == ("cpu", "int8")


def test_explicit_cuda_failure_is_raised(fake_whisper):
    fake_whisper.fail_on = {"cuda"}
    with pytest.raises(RuntimeError):
        tr.transcribe("in.wav", device="cuda")


def test_cuda_available_uses_ctranslate2(monkeypatch):
    monkeypatch.setitem(sys.modules, "ctranslate2", types.SimpleNamespace(get_cuda_device_count=lambda: 2))
    assert tr._cuda_available() is True
    monkeypatch.setitem(sys.modules, "ctranslate2", types.SimpleNamespace(get_cuda_device_count=lambda: 0))
    assert tr._cuda_available() is False

    def boom():
        raise RuntimeError("no driver")

    monkeypatch.setitem(sys.modules, "ctranslate2", types.SimpleNamespace(get_cuda_device_count=boom))
    assert tr._cuda_available() is False


def test_model_libraries_imported_lazily():
    code = (
        "import sys, better_captions.pipeline; "
        "heavy = {'faster_whisper', 'ctranslate2', 'sherpa_onnx', 'pyannote', 'torch'} & set(sys.modules); "
        "assert not heavy, heavy"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_choose_backend_auto_prefers_cuda_then_alternatives(monkeypatch):
    monkeypatch.setattr(tr, "_cuda_available", lambda: True)
    assert tr.choose_backend("auto") == "faster-whisper"
    monkeypatch.setattr(tr, "_cuda_available", lambda: False)
    monkeypatch.setattr(tr, "_backend_available", lambda name: name == "whispercpp")
    assert tr.choose_backend("auto") == "whispercpp"
    monkeypatch.setattr(tr, "_backend_available", lambda name: False)
    assert tr.choose_backend("auto") == "faster-whisper"
    assert tr.choose_backend("mlx") == "mlx"
    with pytest.raises(ValueError):
        tr.choose_backend("nope")
