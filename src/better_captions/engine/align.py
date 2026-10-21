from __future__ import annotations

from better_captions.schema import Segment, SpeakerTurn, Word


def assign_speakers(words: list[Word], turns: list[SpeakerTurn]) -> list[Word]:
    """Set Word.speaker from the turn with the greatest overlap (nearest turn if none overlaps)."""
    raise NotImplementedError


def group_segments(words: list[Word], max_gap: float = 1.0) -> list[Segment]:
    """Group consecutive same-speaker words into sentence-like segments."""
    raise NotImplementedError
