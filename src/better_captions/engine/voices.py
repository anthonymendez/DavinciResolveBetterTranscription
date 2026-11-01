"""A local library of known voices, so recurring speakers come back already named.

Each person the editor confirmed by name in the plugin's review dialog is kept as
``<data>/voices/<safe name>.json``: a few voice centroids (unit-length mean WeSpeaker embeddings, as
``diarize.diarize_detailed`` returns them), one per *source* (the audio they were heard on: a Discord
track, a microphone, a mix), at most ``MAX_CENTROIDS`` per person and embedding model. Saving a voice
again from the same source refines that centroid (a running mean); a new source adds one, dropping the
least recently updated when the person already has ``MAX_CENTROIDS``.

Every centroid records the embedding model that produced it: a different model puts its vectors in a
different space, so ``match`` only compares centroids of the model the current run used.

On a run, ``match`` compares each still-unnamed speaker's centroid (cosine similarity) with every person
in the library, scoring a person by their best centroid. A speaker is named only when the best person
scores at least ``MATCH_THRESHOLD`` *and* beats the next person by ``MATCH_MARGIN``; anything weaker is
left for the transcript-based namer (LLM or heuristic) and the editor. Each person names at most one
speaker per run, and never a speaker when someone in the transcript already carries that name.

The library stays on this machine; ``forget`` (``better-captions voices forget NAME``) deletes a person.
"""

from __future__ import annotations

import json
import os
import re
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from better_captions import paths

LIBRARY_VERSION = 1
VOICES_FILE = "voices.json"  # the run's speaker centroids, next to transcript.json (see write_run_voices)

# Cosine similarity a speaker's centroid needs with a person's for that person's name to be given...
MATCH_THRESHOLD = 0.6
# ...and by how much it must beat the next most similar person (else it is ambiguous: left for the namer).
MATCH_MARGIN = 0.1
# Centroids kept per person and embedding model (one per source: microphone, Discord, mix...).
MAX_CENTROIDS = 5
NAME_SOURCE = "voice"

_UNSAFE = re.compile(r"[^\w.-]+", re.UNICODE)
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
_MAX_STEM = 64


def voices_dir() -> Path:
    return paths.data_dir() / "voices"


def clean_name(name: str) -> str:
    """The name as stored and shown: surrounding and repeated whitespace removed."""
    return re.sub(r"\s+", " ", str(name or "")).strip()


def _key(name: str) -> str:
    """Two names are the same person when they match ignoring case and spacing."""
    return clean_name(name).casefold()


def safe_name(name: str) -> str:
    """A file name stem for ``name`` that is valid on every OS (letters, digits, ``_ . -``), lower case."""
    stem = _UNSAFE.sub("_", clean_name(name).casefold()).strip("._") or "voice"
    stem = stem[:_MAX_STEM].rstrip("._") or "voice"
    if stem.split(".")[0] in _RESERVED:  # CON, NUL, COM1...: not usable as file names on Windows
        stem = "_" + stem
    return stem


def unit(vec) -> np.ndarray | None:
    """``vec`` as a unit-length float64 vector; None when it is empty, zero or not finite."""
    try:
        v = np.asarray(vec, dtype=np.float64).ravel()
    except (TypeError, ValueError):
        return None
    if not v.size or not np.all(np.isfinite(v)):
        return None
    norm = float(np.linalg.norm(v))
    return v / norm if norm > 0 else None


# --- the library -------------------------------------------------------------


@dataclass
class Centroid:
    model: str  # embedding model id (diarize.EMBEDDING_NAME)
    vector: np.ndarray  # unit length
    source: str = ""  # where the voice was heard: a track name, "mix"
    count: int = 1  # how many saves were averaged into it
    updated: float = 0.0  # time.time() of the last save

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "source": self.source,
            "count": self.count,
            "updated": self.updated,
            "vector": [round(float(x), 6) for x in self.vector],
        }

    @classmethod
    def from_dict(cls, data) -> Centroid | None:
        if not isinstance(data, dict) or not isinstance(data.get("model"), str):
            return None
        vector = unit(data.get("vector"))
        if vector is None:
            return None
        try:
            count = max(1, int(data.get("count", 1)))
            updated = float(data.get("updated", 0.0))
        except (TypeError, ValueError):
            count, updated = 1, 0.0
        return cls(data["model"], vector, str(data.get("source") or ""), count, updated)


@dataclass
class Person:
    name: str
    centroids: list[Centroid] = field(default_factory=list)
    path: Path | None = None

    def for_model(self, model: str) -> list[Centroid]:
        return [c for c in self.centroids if c.model == model]

    def to_dict(self) -> dict:
        return {"version": LIBRARY_VERSION, "name": self.name, "centroids": [c.to_dict() for c in self.centroids]}


class VoiceLibrary:
    """The people in ``<data>/voices`` (or ``root``)."""

    def __init__(self, root: str | Path | None = None):
        self.root = Path(root) if root is not None else voices_dir()

    def people(self) -> list[Person]:
        """Everyone saved, sorted by name; unreadable or foreign files are skipped."""
        if not self.root.is_dir():
            return []
        out = []
        for path in sorted(self.root.glob("*.json")):
            person = _read_person(path)
            if person is not None:
                out.append(person)
        return sorted(out, key=lambda p: (_key(p.name), p.name))

    def find(self, name: str) -> Person | None:
        key = _key(name)
        return next((p for p in self.people() if _key(p.name) == key), None)

    def _new_path(self, name: str) -> Path:
        """A file for a new person: ``<safe name>.json``, or ``<safe name>-2.json``... when another person's
        name gives the same file name ("A/B" and "A_B")."""
        stem = safe_name(name)
        path = self.root / f"{stem}.json"
        n = 2
        while path.exists():
            path = self.root / f"{stem}-{n}.json"
            n += 1
        return path

    def save(self, name: str, vector, model: str, source: str = "", now: float | None = None) -> Person:
        """Add one voice centroid to ``name`` (creating them): averaged into their centroid from the same
        model and source, else added as a new one, dropping the least recently updated beyond MAX_CENTROIDS."""
        name = clean_name(name)
        if not name:
            raise ValueError("a voice needs a name")
        vec = unit(vector)
        if vec is None:
            raise ValueError(f"no usable voice centroid for {name!r}")
        now = time.time() if now is None else now
        source = str(source or "")
        person = self.find(name) or Person(name)
        same = [c for c in person.centroids if c.model == model]
        existing = next((c for c in same if c.source == source and c.vector.shape == vec.shape), None)
        if existing is not None:
            mean = unit(existing.vector * existing.count + vec)
            existing.vector = mean if mean is not None else vec
            existing.count += 1
            existing.updated = now
        else:
            person.centroids.append(Centroid(model, vec, source, 1, now))
            same = [c for c in person.centroids if c.model == model]
            if len(same) > MAX_CENTROIDS:
                drop = {id(c) for c in sorted(same, key=lambda c: c.updated)[: len(same) - MAX_CENTROIDS]}
                person.centroids = [c for c in person.centroids if id(c) not in drop]
        person.name = name  # the latest spelling ("anthony" saved as "Anthony")
        self.root.mkdir(parents=True, exist_ok=True)
        if person.path is None:
            person.path = self._new_path(name)
        tmp = person.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(person.to_dict(), indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, person.path)
        return person

    def forget(self, name: str) -> bool:
        """Delete everything saved about ``name``; returns whether there was anything."""
        key = _key(name)
        removed = False
        for person in self.people():
            if _key(person.name) == key and person.path is not None:
                try:
                    person.path.unlink()
                    removed = True
                except FileNotFoundError:
                    pass
        return removed


def _read_person(path: Path) -> Person | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("name"), str) or not clean_name(data["name"]):
        return None
    try:
        if int(data.get("version", LIBRARY_VERSION)) > LIBRARY_VERSION:
            return None
    except (TypeError, ValueError):
        return None
    raw = data.get("centroids")
    centroids = [c for c in (Centroid.from_dict(x) for x in raw) if c is not None] if isinstance(raw, list) else []
    return Person(clean_name(data["name"]), centroids, path)


# --- matching ----------------------------------------------------------------


@dataclass(frozen=True)
class VoiceMatch:
    speaker: str  # the run's speaker id
    name: str  # the person in the library
    score: float  # cosine similarity with that person's closest centroid
    second: float | None  # the next most similar person's score; None when there is nobody else


def score_people(vector, people: Iterable[Person], model: str) -> list[tuple[float, str]]:
    """``(similarity, name)`` for every person with a centroid of ``model``, best first."""
    vec = unit(vector)
    if vec is None:
        return []
    scored = []
    for person in people:
        sims = [float(c.vector @ vec) for c in person.for_model(model) if c.vector.shape == vec.shape]
        if sims:
            scored.append((max(sims), person.name))
    return sorted(scored, key=lambda x: (-x[0], _key(x[1])))


def match(
    centroids: dict[str, object],
    model: str,
    people: Iterable[Person],
    taken: Iterable[str] = (),
    threshold: float = MATCH_THRESHOLD,
    margin: float = MATCH_MARGIN,
) -> dict[str, VoiceMatch]:
    """speaker id -> the person whose voice it clearly is (see the module docs). ``centroids`` are the run's
    speakers to look up (speaker id -> centroid); ``taken`` the names already used in the transcript."""
    people = list(people)
    if not people or not model:
        return {}
    candidates = []
    for speaker, vector in centroids.items():
        ranked = score_people(vector, people, model)
        if not ranked:
            continue
        best, name = ranked[0]
        second = ranked[1][0] if len(ranked) > 1 else None
        if best >= threshold and (second is None or best - second >= margin):
            candidates.append(VoiceMatch(speaker, name, best, second))
    used = {_key(n) for n in taken if n}
    out: dict[str, VoiceMatch] = {}
    for m in sorted(candidates, key=lambda m: (-m.score, m.speaker)):  # one speaker per person: the closest
        if _key(m.name) in used:
            continue
        used.add(_key(m.name))
        out[m.speaker] = m
    return out


def confidence(m: VoiceMatch) -> float:
    """``Speaker.name_confidence`` for a voice match: its cosine similarity, within 0..1."""
    return round(min(1.0, max(0.0, m.score)), 3)


# --- the run's voices (voices.json next to transcript.json) --------------------


@dataclass
class RunVoice:
    centroid: np.ndarray
    source: str = ""


def write_run_voices(out_dir: str | Path, model: str | None, voices: dict[str, RunVoice]) -> Path | None:
    """Write ``voices.json``: each speaker's voice centroid from this run, for ``voices save`` to keep the
    ones the editor confirms. Nothing is written (and an old file is removed) when there are none."""
    path = Path(out_dir) / VOICES_FILE
    usable = {sid: v for sid, v in voices.items() if unit(v.centroid) is not None}
    if not model or not usable:
        path.unlink(missing_ok=True)
        return None
    data = {
        "version": LIBRARY_VERSION,
        "model": model,
        "speakers": {
            sid: {"source": v.source, "centroid": [round(float(x), 6) for x in unit(v.centroid)]}
            for sid, v in usable.items()
        },
    }
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    return path


def read_run_voices(out_dir: str | Path) -> tuple[str, dict[str, RunVoice]]:
    """``(model, speaker id -> RunVoice)`` from a run's ``voices.json`` (``out_dir`` or the file itself).
    Raises ValueError when it is missing or malformed."""
    path = Path(out_dir)
    if path.is_dir():
        path = path / VOICES_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"no voices saved by this run ({path} is missing)") from exc
    except (OSError, ValueError) as exc:
        raise ValueError(f"could not read {path}: {exc}") from exc
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("model"), str)
        or not isinstance(data.get("speakers"), dict)
    ):
        raise ValueError(f"{path} is not a voices file")
    out = {}
    for sid, entry in data["speakers"].items():
        vec = unit(entry.get("centroid")) if isinstance(entry, dict) else None
        if vec is not None:
            out[str(sid)] = RunVoice(vec, str(entry.get("source") or ""))
    return data["model"], out


def save_confirmed(
    out_dir: str | Path, names: dict[str, str], library: VoiceLibrary | None = None
) -> tuple[list[dict], list[dict]]:
    """Keep the voices of a run's speakers the editor confirmed: ``names`` maps speaker id -> name.
    Returns ``(saved, skipped)``: ``{"speaker", "name"[, "source"]}`` / ``{"speaker", "name", "reason"}``."""
    model, voices = read_run_voices(out_dir)
    library = library or VoiceLibrary()
    saved, skipped = [], []
    for sid, name in names.items():
        name = clean_name(name)
        if not name:
            skipped.append({"speaker": sid, "name": name, "reason": "no name"})
        elif sid not in voices:
            skipped.append({"speaker": sid, "name": name, "reason": "no voice for this speaker"})
        else:
            library.save(name, voices[sid].centroid, model, voices[sid].source)
            saved.append({"speaker": sid, "name": name, "source": voices[sid].source})
    return saved, skipped
