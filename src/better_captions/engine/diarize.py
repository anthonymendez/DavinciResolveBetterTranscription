from __future__ import annotations

from typing import Callable

from better_captions.schema import SpeakerTurn

ProgressFn = Callable[[float], None]


def diarize(
    audio_path: str,
    num_speakers: int | None = None,
    min_speakers: int | None = None,
    max_speakers: int | None = None,
    backend: str = "sherpa",
    progress: ProgressFn | None = None,
) -> list[SpeakerTurn]:
    """Return speaker turns sorted by start, labelled SPEAKER_00, SPEAKER_01, ... in order of first appearance."""
    raise NotImplementedError
