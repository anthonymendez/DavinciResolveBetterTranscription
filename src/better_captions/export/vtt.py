from __future__ import annotations

from better_captions.export.srt import format_timestamp
from better_captions.schema import Cue


def to_vtt(cues: list[Cue]) -> str:
    blocks = [f"{format_timestamp(cue.start, '.')} --> {format_timestamp(cue.end, '.')}\n{cue.text}\n" for cue in cues]
    return "WEBVTT\n\n" + "\n".join(blocks)
