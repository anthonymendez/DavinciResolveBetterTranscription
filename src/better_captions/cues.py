"""Turn segments into readable on-screen caption cues."""

from __future__ import annotations

from dataclasses import dataclass

from better_captions.schema import Cue, Transcript


@dataclass
class CueOptions:
    max_chars_per_line: int = 42
    max_lines: int = 2
    max_duration: float = 6.0
    min_duration: float = 0.8
    min_gap: float = 0.05  # seconds between consecutive cues of the same speaker


def build_cues(transcript: Transcript, options: CueOptions | None = None) -> list[Cue]:
    """Split each segment at word boundaries into cues obeying options; sorted by start."""
    raise NotImplementedError
