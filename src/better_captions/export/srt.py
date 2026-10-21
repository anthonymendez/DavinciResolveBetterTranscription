from __future__ import annotations

from pathlib import Path

from better_captions.schema import Cue, Transcript


def to_srt(cues: list[Cue]) -> str:
    raise NotImplementedError


def write_speaker_srts(transcript: Transcript, out_dir: str | Path) -> dict[str, Path]:
    """Write one SRT per speaker (<out_dir>/<speaker_id>.srt) from transcript.cues; return {speaker_id: path}."""
    raise NotImplementedError
