"""mlx backend with mlx_whisper replaced by a fake module and the platform faked (runs anywhere)."""

import importlib.util
import sys
import types

import pytest

from better_captions.engine import transcribe as tr
from better_captions.engine.backends import mlx


class FakeMlxWhisper:
    def __init__(self, language="en"):
        self.calls = []
        self.loaded = []
        self.language = language

    def load_audio(self, path):
        self.loaded.append(path)
        return [0.0] * 160  # 10 s at the fake 16 Hz sample rate

    def transcribe(self, audio, **kwargs):
        self.calls.append((audio, kwargs))
        return {
            "text": "Hello world. Bye",
            "language": self.language if kwargs.get("language") is None else kwargs["language"],
            "segments": [
                {
                    "end": 2.0,
                    "words": [
                        {"word": " Hello", "start": 0.0, "end": 0.5, "probability": 0.9},
                        {"word": " world.", "start": 0.6, "end": 2.0, "probability": 0.8},
                    ],
                },
                {
                    "end": 5.0,
                    "words": [
                        {"word": " ", "start": 2.5, "end": 2.6, "probability": 0.1},
                        {"word": " Bye", "start": 4.0, "end": 5.0, "probability": 0.7},
                    ],
                },
                {"end": 10.5, "words": []},
                {"end": 10.5},
            ],
        }


@pytest.fixture
def fake_mlx(monkeypatch):
    fake = FakeMlxWhisper()
    audio_mod = types.SimpleNamespace(SAMPLE_RATE=16, load_audio=fake.load_audio)
    monkeypatch.setitem(sys.modules, "mlx_whisper", types.SimpleNamespace(transcribe=fake.transcribe, audio=audio_mod))
    monkeypatch.setitem(sys.modules, "mlx_whisper.audio", audio_mod)
    monkeypatch.setattr(mlx, "_load_audio", lambda path, sr: fake.load_audio(path))
    return fake


def test_words_converted_stripped_and_empty_skipped(fake_mlx):
    language, duration, words = mlx.transcribe("in.wav", model="small", language="en")
    assert (language, duration) == ("en", 10.0)
    assert [(w.text, w.start, w.end, w.probability, w.speaker) for w in words] == [
        ("Hello", 0.0, 0.5, 0.9, None),
        ("world.", 0.6, 2.0, 0.8, None),
        ("Bye", 4.0, 5.0, 0.7, None),
    ]
    assert fake_mlx.loaded == ["in.wav"]
    audio, kwargs = fake_mlx.calls[0]
    assert len(audio) == 160
    assert kwargs == {"path_or_hf_repo": "mlx-community/whisper-small-mlx", "language": "en", "word_timestamps": True}


@pytest.mark.parametrize(
    "model,repo",
    [
        ("large-v3-turbo", "mlx-community/whisper-large-v3-turbo"),
        ("large-v3", "mlx-community/whisper-large-v3-mlx"),
        ("medium", "mlx-community/whisper-medium-mlx"),
        ("small", "mlx-community/whisper-small-mlx"),
        ("base", "mlx-community/whisper-base-mlx"),
        ("tiny", "mlx-community/whisper-tiny-mlx"),
        ("someone/custom-whisper-mlx", "someone/custom-whisper-mlx"),
    ],
)
def test_model_mapping(fake_mlx, model, repo):
    mlx.transcribe("in.wav", model=model)
    assert fake_mlx.calls[0][1]["path_or_hf_repo"] == repo


def test_progress_from_segment_end_over_duration(fake_mlx):
    seen = []
    mlx.transcribe("in.wav", progress=seen.append)
    assert seen == [0.2, 0.5, 1.0, 1.0]  # segments ending past the duration are clamped


def test_language_detected_when_not_given(fake_mlx):
    fake_mlx.language = "es"
    language, _, _ = mlx.transcribe("in.wav", language=None)
    assert language == "es"
    assert fake_mlx.calls[0][1]["language"] is None


def test_dispatched_from_engine_transcribe(fake_mlx):
    language, duration, words = tr.transcribe("in.wav", model="tiny", backend="mlx")
    assert (language, duration, len(words)) == ("en", 10.0, 3)
    assert fake_mlx.calls[0][1]["path_or_hf_repo"] == "mlx-community/whisper-tiny-mlx"


@pytest.mark.parametrize(
    "plat,machine,installed,expected",
    [
        ("darwin", "arm64", True, True),
        ("darwin", "arm64", False, False),
        ("darwin", "x86_64", True, False),
        ("linux", "arm64", True, False),
        ("win32", "AMD64", True, False),
    ],
)
def test_available(monkeypatch, plat, machine, installed, expected):
    monkeypatch.setattr(mlx.sys, "platform", plat)
    monkeypatch.setattr(mlx.platform, "machine", lambda: machine)
    looked_up = []

    def find_spec(name):
        looked_up.append(name)
        return object() if installed else None

    monkeypatch.setattr(importlib.util, "find_spec", find_spec)
    assert mlx.available() is expected
    assert looked_up in ([], ["mlx_whisper"])


def test_available_tolerates_find_spec_errors(monkeypatch):
    monkeypatch.setattr(mlx.sys, "platform", "darwin")
    monkeypatch.setattr(mlx.platform, "machine", lambda: "arm64")

    def boom(name):
        raise ValueError("broken spec")

    monkeypatch.setattr(importlib.util, "find_spec", boom)
    assert mlx.available() is False


def test_engine_reports_mlx_availability(monkeypatch):
    monkeypatch.setattr(mlx, "available", lambda: True)
    assert tr._backend_available("mlx") is True
    monkeypatch.setattr(mlx, "available", lambda: False)
    assert tr._backend_available("mlx") is False


def test_load_audio_needs_no_ffmpeg(tmp_path, monkeypatch):
    """Decoding goes through soundfile (resampled to 16 kHz mono), never mlx-whisper's ffmpeg loader."""
    import numpy as np
    import soundfile as sf

    path = tmp_path / "a.wav"
    sf.write(path, np.zeros((48000, 2), dtype="float32"), 48000)  # 1 s stereo at 48 kHz, like a Resolve render
    monkeypatch.setenv("PATH", "")  # no ffmpeg reachable
    audio = mlx._load_audio(str(path), 16000)
    assert audio.ndim == 1 and abs(len(audio) - 16000) <= 1
