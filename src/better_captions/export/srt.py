from __future__ import annotations

from pathlib import Path

from better_captions.schema import Cue, Transcript


def format_timestamp(seconds: float, separator: str = ",") -> str:
    """HH:MM:SS<separator>mmm; SRT uses ',' and WebVTT uses '.'. Hours are not capped at 23."""
    ms = max(0, round(seconds * 1000))
    hours, rest = divmod(ms, 3_600_000)
    minutes, rest = divmod(rest, 60_000)
    secs, millis = divmod(rest, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{separator}{millis:03d}"


def to_srt(cues: list[Cue]) -> str:
    blocks = [
        f"{index}\n{format_timestamp(cue.start)} --> {format_timestamp(cue.end)}\n{cue.text}\n"
        for index, cue in enumerate(cues, start=1)
    ]
    return "\n".join(blocks)


def write_speaker_srts(transcript: Transcript, out_dir: str | Path) -> dict[str, Path]:
    """Write one SRT per speaker (<out_dir>/<speaker_id>.srt) from transcript.cues; return {speaker_id: path}."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    by_speaker: dict[str, list[Cue]] = {}
    for cue in transcript.cues:
        by_speaker.setdefault(cue.speaker, []).append(cue)
    paths: dict[str, Path] = {}
    for speaker_id, cues in by_speaker.items():
        path = out / f"{speaker_id}.srt"
        path.write_text(to_srt(cues), encoding="utf-8", newline="\n")
        paths[speaker_id] = path
    return paths
