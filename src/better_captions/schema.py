"""Shared data contract between the engine and the Resolve plugin.

The engine writes a ``Transcript`` as ``transcript.json``; the Resolve plugin
(stdlib-only, may run on a different Python) reads the same JSON. Keep this
module dependency-free and bump SCHEMA_VERSION on breaking changes.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1


@dataclass
class Word:
    text: str
    start: float  # seconds from the start of the audio
    end: float
    probability: float = 1.0
    speaker: str | None = None  # Speaker.id, set by alignment


@dataclass
class SpeakerTurn:
    """A diarization result: one speaker talking over [start, end)."""

    speaker: str
    start: float
    end: float


@dataclass
class Segment:
    """A run of consecutive words from one speaker (roughly a sentence)."""

    speaker: str
    start: float
    end: float
    text: str
    words: list[Word] = field(default_factory=list)


@dataclass
class Speaker:
    id: str  # stable diarization label, e.g. "SPEAKER_00"
    name: str | None = None  # human name inferred from context, if any
    name_confidence: float = 0.0  # 0..1
    name_source: str | None = None  # "heuristic", "llm", "user"

    @property
    def display_name(self) -> str:
        return self.name or self.id


@dataclass
class Cue:
    """One on-screen caption: what ends up as an SRT entry or a Text+ clip."""

    speaker: str
    start: float
    end: float
    lines: list[str]

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


@dataclass
class Transcript:
    language: str
    duration: float
    speakers: list[Speaker] = field(default_factory=list)
    segments: list[Segment] = field(default_factory=list)
    cues: list[Cue] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION

    def speaker(self, speaker_id: str) -> Speaker | None:
        return next((s for s in self.speakers if s.id == speaker_id), None)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Transcript:
        version = data.get("schema_version", SCHEMA_VERSION)
        if version > SCHEMA_VERSION:
            raise ValueError(f"transcript schema {version} is newer than supported {SCHEMA_VERSION}")
        return cls(
            language=data["language"],
            duration=data["duration"],
            speakers=[Speaker(**s) for s in data.get("speakers", [])],
            segments=[
                Segment(**{**s, "words": [Word(**w) for w in s.get("words", [])]}) for s in data.get("segments", [])
            ],
            cues=[Cue(**c) for c in data.get("cues", [])],
            meta=data.get("meta", {}),
            schema_version=version,
        )

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> Transcript:
        return cls.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
