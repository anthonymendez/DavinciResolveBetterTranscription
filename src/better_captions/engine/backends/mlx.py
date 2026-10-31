"""Apple Silicon backend: mlx-whisper runs Whisper on the Metal GPU via MLX."""

from __future__ import annotations

import importlib.util
import platform
import sys

from better_captions.engine.transcribe import ProgressFn, ThrottledProgress
from better_captions.schema import Word

# Our model names -> mlx-community Hugging Face repos; anything else is passed through as a repo id or local path.
MODEL_REPOS = {
    "large-v3-turbo": "mlx-community/whisper-large-v3-turbo",
    "large-v3": "mlx-community/whisper-large-v3-mlx",
    "medium": "mlx-community/whisper-medium-mlx",
    "small": "mlx-community/whisper-small-mlx",
    "base": "mlx-community/whisper-base-mlx",
    "tiny": "mlx-community/whisper-tiny-mlx",
}


def available() -> bool:
    """macOS on Apple Silicon with mlx-whisper installed (no import of MLX, no downloads)."""
    if sys.platform != "darwin" or platform.machine() != "arm64":
        return False
    try:
        return importlib.util.find_spec("mlx_whisper") is not None
    except (ImportError, ValueError):
        return False


def model_repo(model: str) -> str:
    return MODEL_REPOS.get(model, model)


def transcribe(
    audio_path: str,
    model: str = "large-v3-turbo",
    language: str | None = None,
    device: str = "auto",
    progress: ProgressFn | None = None,
) -> tuple[str, float, list[Word]]:
    """Return (language, duration_seconds, words) like engine.transcribe; ``device`` is ignored (MLX picks the GPU)."""
    import mlx_whisper
    from mlx_whisper.audio import SAMPLE_RATE

    # mlx-whisper reports no duration, so decode the audio ourselves and hand it the samples.
    # Its own loader shells out to ffmpeg, which most Macs don't have; ours needs only soundfile.
    audio = _load_audio(audio_path, SAMPLE_RATE)
    duration = len(audio) / SAMPLE_RATE
    result = mlx_whisper.transcribe(audio, path_or_hf_repo=model_repo(model), language=language, word_timestamps=True)
    # mlx-whisper has no progress callback (only a tqdm bar), so progress advances per returned segment.
    report = ThrottledProgress(progress)
    words: list[Word] = []
    for seg in result.get("segments") or []:
        for w in seg.get("words") or []:
            text = w["word"].strip()
            if text:
                words.append(
                    Word(text=text, start=float(w["start"]), end=float(w["end"]), probability=float(w["probability"]))
                )
        if duration > 0:
            report(float(seg["end"]) / duration)
    return result.get("language") or language or "", duration, words


def _load_audio(path: str, sample_rate: int):
    from better_captions.engine.diarize import load_audio

    return load_audio(path, sample_rate)
