from __future__ import annotations

from typing import Callable

from better_captions.schema import Word

ProgressFn = Callable[[float], None]  # fraction 0..1


def transcribe(
    audio_path: str,
    model: str = "large-v3-turbo",
    language: str | None = None,
    device: str = "auto",
    progress: ProgressFn | None = None,
) -> tuple[str, float, list[Word]]:
    """Return (language, duration_seconds, words) with word-level timestamps; Word.speaker is None."""
    raise NotImplementedError
