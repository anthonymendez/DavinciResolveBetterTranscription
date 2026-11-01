"""Read the engine's ``transcript.json`` (schema in ``src/better_captions/schema.py``) without importing it,
and re-write the per-speaker SRTs when speaker edits changed who says what."""

from __future__ import annotations

import json
import math
import os
from dataclasses import dataclass, field

SUPPORTED_SCHEMA_VERSION = 1
# A cue whose ``speaker_confidence`` is below this is "uncertain": the voice match for that line was
# ambiguous, so the review dialogs list it first for the user to check. Unknown confidence is not uncertain.
UNCERTAIN_BELOW = 0.6


class TranscriptError(ValueError):
    pass


@dataclass
class Speaker:
    id: str
    name: str | None = None
    name_confidence: float = 0.0
    name_source: str | None = None

    @property
    def display_name(self) -> str:
        return self.name or self.id


@dataclass
class Cue:
    speaker: str
    start: float
    end: float
    lines: list[str] = field(default_factory=list)
    speaker_confidence: float | None = None  # 0..1, how sure the voice match is; None: unknown

    @property
    def text(self) -> str:
        return "\n".join(self.lines)

    @property
    def uncertain(self) -> bool:
        return is_uncertain(self)


@dataclass
class Word:
    """A timed word from ``segments[].words`` (whisper keeps a leading space in ``text``)."""

    text: str
    start: float
    end: float
    speaker: str | None = None


@dataclass
class Transcript:
    language: str
    duration: float
    speakers: list[Speaker] = field(default_factory=list)
    cues: list[Cue] = field(default_factory=list)
    meta: dict = field(default_factory=dict)
    words: list[Word] = field(default_factory=list)  # every segment's words, sorted by start

    def speaker(self, speaker_id: str) -> Speaker | None:
        for speaker in self.speakers:
            if speaker.id == speaker_id:
                return speaker
        return None

    def cues_for(self, speaker_id: str) -> list[Cue]:
        return sorted((c for c in self.cues if c.speaker == speaker_id), key=lambda c: (c.start, c.end))

    def speaker_ids_with_cues(self) -> list[str]:
        """Speaker ids in transcript order, keeping only those that have at least one cue."""
        with_cues = {c.speaker for c in self.cues}
        ordered = [s.id for s in self.speakers if s.id in with_cues]
        ordered += sorted(sid for sid in with_cues if sid not in ordered)
        return ordered


def parse_confidence(value) -> float | None:
    """A cue's ``speaker_confidence`` as a float in 0..1, or None when it is missing or not one."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and 0.0 <= number <= 1.0 else None


def is_uncertain(cue) -> bool:
    """True when ``cue`` (a Cue, a CueSummary or a raw cue dict) has a known confidence below UNCERTAIN_BELOW."""
    value = cue.get("speaker_confidence") if isinstance(cue, dict) else getattr(cue, "speaker_confidence", None)
    confidence = parse_confidence(value)
    return confidence is not None and confidence < UNCERTAIN_BELOW


def from_dict(data: dict) -> Transcript:
    version = int(data.get("schema_version", SUPPORTED_SCHEMA_VERSION))
    if version > SUPPORTED_SCHEMA_VERSION:
        raise TranscriptError(
            "transcript schema %d is newer than this plugin supports (%d); re-run the installer"
            % (version, SUPPORTED_SCHEMA_VERSION)
        )
    speakers = [
        Speaker(
            id=str(s["id"]),
            name=s.get("name") or None,
            name_confidence=float(s.get("name_confidence", 0.0) or 0.0),
            name_source=s.get("name_source"),
        )
        for s in data.get("speakers", [])
    ]
    cues = []
    for c in data.get("cues", []):
        lines = c.get("lines")
        if lines is None:
            lines = str(c.get("text", "")).split("\n")
        cues.append(
            Cue(
                speaker=str(c["speaker"]),
                start=float(c["start"]),
                end=float(c["end"]),
                lines=[str(x) for x in lines],
                speaker_confidence=parse_confidence(c.get("speaker_confidence")),
            )
        )
    words = []
    for segment in data.get("segments", []) or []:
        for w in segment.get("words", []) or []:
            try:
                start, end = float(w["start"]), float(w["end"])
            except (KeyError, TypeError, ValueError):
                continue
            if not str(w.get("text", "")).strip():
                continue
            speaker = segment.get("speaker") or w.get("speaker")  # speaker edits relabel segments
            words.append(Word(text=str(w["text"]), start=start, end=end, speaker=str(speaker) if speaker else None))
    words.sort(key=lambda w: (w.start, w.end))
    return Transcript(
        language=str(data.get("language", "")),
        duration=float(data.get("duration", 0.0) or 0.0),
        speakers=speakers,
        cues=cues,
        meta=dict(data.get("meta", {}) or {}),
        words=words,
    )


def load_dict(path) -> dict:
    """The raw transcript object (validated by ``from_dict``), for editing with ``speaker_edits``."""
    with open(str(path), encoding="utf-8") as handle:
        try:
            data = json.load(handle)
        except ValueError as exc:
            raise TranscriptError("%s is not valid JSON: %s" % (path, exc)) from exc
    if not isinstance(data, dict):
        raise TranscriptError("%s does not contain a transcript object" % path)
    from_dict(data)
    return data


def load(path) -> Transcript:
    return from_dict(load_dict(path))


def format_timestamp(seconds: float) -> str:
    """SRT time, HH:MM:SS,mmm (same as the engine's export/srt.py)."""
    ms = max(0, int(round(seconds * 1000)))
    hours, rest = divmod(ms, 3600000)
    minutes, rest = divmod(rest, 60000)
    secs, millis = divmod(rest, 1000)
    return "%02d:%02d:%02d,%03d" % (hours, minutes, secs, millis)


def to_srt(cues: list[Cue]) -> str:
    blocks = [
        "%d\n%s --> %s\n%s\n" % (index, format_timestamp(cue.start), format_timestamp(cue.end), cue.text)
        for index, cue in enumerate(cues, start=1)
    ]
    return "\n".join(blocks)


def write_speaker_srts(transcript: Transcript, out_dir: str) -> dict[str, str]:
    """One SRT per speaker (<out_dir>/<speaker_id>.srt), as the engine writes them; {speaker_id: path}."""
    os.makedirs(out_dir, exist_ok=True)
    paths = {}
    for speaker_id in transcript.speaker_ids_with_cues():
        path = os.path.join(out_dir, speaker_id + ".srt")
        with open(path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(to_srt(transcript.cues_for(speaker_id)))
        paths[speaker_id] = path
    return paths
