"""Fix speaker-detection mistakes before tracks are written (pure functions; stdlib only).

Edits are operations on the engine's transcript dict (the parsed ``transcript.json``):

- ``rename``: give a speaker a name (``name_source`` becomes ``"user"``)
- ``merge``: speaker B was really speaker A; B's cues, segments and words are relabelled to A,
  B disappears and A keeps its name
- ``reassign``: some cues belong to another existing speaker
- ``split``: some cues belong to a speaker diarization missed; they move to a new speaker

``SpeakerEditor`` keeps the original transcript and the list of operations (the edit stack);
the current transcript is the original with every operation replayed, so ``undo`` just drops
the last one. Operations are plain JSON and refer to cues by index *and* time, so they can be
saved as ``edits.json`` next to the run's ``transcript.json`` and re-applied to a re-run on the
same audio (a cue is found again by its times when the index no longer matches).
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import re
from dataclasses import dataclass

from better_captions_plugin import transcript as transcript_mod

EDITS_FILE = "edits.json"
EDITS_VERSION = 1
TIME_TOLERANCE = 0.05  # seconds; how far a saved cue reference may drift and still match
STRUCTURAL_OPS = ("merge", "reassign", "split")


class EditError(ValueError):
    pass


@dataclass(frozen=True)
class SpeakerSummary:
    id: str
    name: str  # display name: the speaker's name, or the id
    cue_count: int
    sample: str  # first non-empty cue text
    source: "str | None" = None  # name_source


@dataclass(frozen=True)
class CueSummary:
    index: int  # position in the transcript's cue list (stable: edits never add or remove cues)
    start: float
    end: float
    speaker: str
    text: str


# --- operations on the transcript dict


def _speaker_ids(data: dict) -> "list[str]":
    """Known speaker ids: the speaker list, then ids that only appear on cues."""
    ids = [str(s.get("id")) for s in data.get("speakers", [])]
    for cue in data.get("cues", []):
        if str(cue.get("speaker")) not in ids:
            ids.append(str(cue.get("speaker")))
    return ids


def _speaker_entry(data: dict, speaker_id: str, create: bool = False) -> "dict | None":
    for speaker in data.setdefault("speakers", []):
        if str(speaker.get("id")) == speaker_id:
            return speaker
    if not create:
        return None
    entry = {"id": speaker_id, "name": None, "name_confidence": 0.0, "name_source": None}
    data["speakers"].append(entry)
    return entry


def _require_speaker(data: dict, speaker_id: str):
    if speaker_id not in _speaker_ids(data):
        raise EditError("unknown speaker %s" % speaker_id)


def _find_cue(cues: list, ref: dict) -> int:
    """Index of the cue ``ref`` points at: its index if the times still match, else the closest by time."""
    start, end = float(ref["start"]), float(ref["end"])

    def matches(cue):
        return abs(float(cue["start"]) - start) <= TIME_TOLERANCE and abs(float(cue["end"]) - end) <= TIME_TOLERANCE

    index = ref.get("index")
    if isinstance(index, int) and 0 <= index < len(cues) and matches(cues[index]):
        return index
    candidates = [i for i, cue in enumerate(cues) if matches(cue)]
    if not candidates:
        raise EditError("no cue at %.2f-%.2f s" % (start, end))
    return min(candidates, key=lambda i: abs(float(cues[i]["start"]) - start) + abs(float(cues[i]["end"]) - end))


def _relabel_segments(data: dict, old: str, new: str, start: "float | None" = None, end: "float | None" = None):
    """Relabel ``old``'s segments (all, or those whose midpoint lies in [start, end]) and their words."""
    for segment in data.get("segments", []) or []:
        if str(segment.get("speaker")) != old:
            continue
        if start is not None:
            middle = (float(segment.get("start", 0.0)) + float(segment.get("end", 0.0))) / 2.0
            if not start <= middle <= end:
                continue
        segment["speaker"] = new
        for word in segment.get("words", []) or []:
            if word.get("speaker") in (None, old):
                word["speaker"] = new


def _move_cues(data: dict, refs: list, target: str):
    cues = data.get("cues", [])
    indexes = [_find_cue(cues, ref) for ref in refs]
    if not indexes:
        raise EditError("no cues selected")
    for index in indexes:
        cue = cues[index]
        old = str(cue["speaker"])
        if old == target:
            continue
        cue["speaker"] = target
        _relabel_segments(data, old, target, float(cue["start"]), float(cue["end"]))


def _apply_rename(data: dict, op: dict):
    speaker_id = str(op["speaker"])
    _require_speaker(data, speaker_id)
    name = re.sub(r"\s+", " ", str(op.get("name") or "")).strip()
    entry = _speaker_entry(data, speaker_id, create=True)
    entry["name"] = name or None
    entry["name_confidence"] = 1.0 if name else 0.0
    entry["name_source"] = "user" if name else None


def _apply_merge(data: dict, op: dict):
    source, target = str(op["source"]), str(op["target"])
    if source == target:
        raise EditError("cannot merge a speaker into itself")
    _require_speaker(data, source)
    _require_speaker(data, target)
    _speaker_entry(data, target, create=True)
    for cue in data.get("cues", []):
        if str(cue.get("speaker")) == source:
            cue["speaker"] = target
    _relabel_segments(data, source, target)
    data["speakers"] = [s for s in data.get("speakers", []) if str(s.get("id")) != source]


def _apply_reassign(data: dict, op: dict):
    target = str(op["speaker"])
    _require_speaker(data, target)
    _speaker_entry(data, target, create=True)
    _move_cues(data, op.get("cues", []), target)


def _apply_split(data: dict, op: dict):
    new_id = str(op["speaker"])
    if new_id in _speaker_ids(data):
        raise EditError("speaker %s already exists" % new_id)
    name = re.sub(r"\s+", " ", str(op.get("name") or "")).strip() or None
    data.setdefault("speakers", []).append(
        {"id": new_id, "name": name, "name_confidence": 1.0 if name else 0.0, "name_source": "user" if name else None}
    )
    _move_cues(data, op.get("cues", []), new_id)


_APPLY = {"rename": _apply_rename, "merge": _apply_merge, "reassign": _apply_reassign, "split": _apply_split}


def apply_op(data: dict, op: dict):
    """Apply one operation to ``data`` in place; EditError (and nothing changed) when it does not fit."""
    kind = op.get("op")
    if kind not in _APPLY:
        raise EditError("unknown edit %r" % (kind,))
    trial = copy.deepcopy(data)
    try:
        _APPLY[kind](trial, op)
    except (KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, EditError):
            raise
        raise EditError("malformed %s edit: %s" % (kind, exc))
    data.clear()
    data.update(trial)


def new_speaker_id(data: dict) -> str:
    """The first free ``SPEAKER_NN`` id."""
    taken = set(_speaker_ids(data))
    number = 0
    while "SPEAKER_%02d" % number in taken:
        number += 1
    return "SPEAKER_%02d" % number


# --- the editor


class SpeakerEditor:
    """The transcript being reviewed plus its edit stack."""

    def __init__(self, transcript: dict):
        self.base = copy.deepcopy(transcript)
        self.ops: "list[dict]" = []
        self.skipped: "list[tuple[dict, str]]" = []  # (op, reason) for saved edits that no longer apply
        self.data = copy.deepcopy(self.base)

    # queries

    def speakers(self) -> "list[SpeakerSummary]":
        """Speakers with at least one cue, in transcript order (as ``Transcript.speaker_ids_with_cues``)."""
        cues = self.data.get("cues", [])
        with_cues = {str(c.get("speaker")) for c in cues}
        ordered = [sid for sid in (str(s.get("id")) for s in self.data.get("speakers", [])) if sid in with_cues]
        ordered += sorted(sid for sid in with_cues if sid not in ordered)
        summaries = []
        for speaker_id in ordered:
            own = sorted(
                (c for c in cues if str(c.get("speaker")) == speaker_id),
                key=lambda c: (float(c["start"]), float(c["end"])),
            )
            texts = [_cue_text(c) for c in own]
            entry = _speaker_entry(self.data, speaker_id) or {}
            summaries.append(
                SpeakerSummary(
                    id=speaker_id,
                    name=entry.get("name") or speaker_id,
                    cue_count=len(own),
                    sample=next((t for t in texts if t.strip()), ""),
                    source=entry.get("name_source"),
                )
            )
        return summaries

    def cues(self, speaker: "str | None" = None) -> "list[CueSummary]":
        """Cues in time order, optionally only one speaker's."""
        rows = [
            CueSummary(
                index=i, start=float(c["start"]), end=float(c["end"]), speaker=str(c["speaker"]), text=_cue_text(c)
            )
            for i, c in enumerate(self.data.get("cues", []))
            if speaker is None or str(c["speaker"]) == speaker
        ]
        return sorted(rows, key=lambda r: (r.start, r.end, r.index))

    def display_name(self, speaker_id: str) -> str:
        entry = _speaker_entry(self.data, speaker_id) or {}
        return entry.get("name") or speaker_id

    def transcript(self) -> transcript_mod.Transcript:
        return transcript_mod.from_dict(self.data)

    @property
    def can_undo(self) -> bool:
        return bool(self.ops)

    @property
    def relabelled(self) -> bool:
        """True when cues changed speaker (so the engine's per-speaker SRTs are stale)."""
        return any(op.get("op") in STRUCTURAL_OPS for op in self.ops)

    # operations; each is one undo step

    def rename(self, speaker_id: str, name: str) -> dict:
        return self._push({"op": "rename", "speaker": speaker_id, "name": str(name or "").strip()})

    def merge(self, source: str, target: str) -> dict:
        """Merge speaker ``source`` (B) into ``target`` (A): B's cues become A's, A's name is kept."""
        return self._push({"op": "merge", "source": source, "target": target})

    def reassign(self, cue_indexes, speaker: str) -> dict:
        """Move cues (indexes into the cue list) to an existing speaker."""
        return self._push({"op": "reassign", "cues": self._refs(cue_indexes), "speaker": speaker})

    def split(self, cue_indexes, name: "str | None" = None) -> str:
        """Move cues to a new speaker; returns the new speaker's id."""
        new_id = new_speaker_id(self.data)
        self._push(
            {"op": "split", "cues": self._refs(cue_indexes), "speaker": new_id, "name": (name or "").strip() or None}
        )
        return new_id

    def record_names(self, names: "dict[str, str]") -> "list[dict]":
        """Turn the review dialog's names into rename edits, for those that differ from the current name."""
        pushed = []
        known = {s.id for s in self.speakers()}
        for speaker_id, name in names.items():
            name = re.sub(r"\s+", " ", str(name or "")).strip()
            if speaker_id in known and name and name != self.display_name(speaker_id):
                pushed.append(self.rename(speaker_id, name))
        return pushed

    def undo(self) -> "dict | None":
        if not self.ops:
            return None
        op = self.ops.pop()
        self._replay()
        return op

    def reapply(self, ops) -> int:
        """Apply saved operations, skipping those that no longer fit; returns how many applied."""
        applied = 0
        for op in ops or []:
            if not isinstance(op, dict):
                self.skipped.append((op, "not an edit"))
                continue
            try:
                self._push(copy.deepcopy(op))
                applied += 1
            except EditError as exc:
                self.skipped.append((op, str(exc)))
        return applied

    def _refs(self, cue_indexes) -> "list[dict]":
        cues = self.data.get("cues", [])
        refs = []
        for index in cue_indexes:
            index = int(index)
            if not 0 <= index < len(cues):
                raise EditError("no cue #%d" % index)
            refs.append({"index": index, "start": float(cues[index]["start"]), "end": float(cues[index]["end"])})
        if not refs:
            raise EditError("no cues selected")
        return refs

    def _push(self, op: dict) -> dict:
        apply_op(self.data, op)
        self.ops.append(op)
        return op

    def _replay(self):
        data = copy.deepcopy(self.base)
        for op in self.ops:
            apply_op(data, op)
        self.data = data


def _cue_text(cue: dict) -> str:
    lines = cue.get("lines")
    if lines is None:
        return str(cue.get("text", ""))
    return "\n".join(str(x) for x in lines)


# --- edits.json


def audio_fingerprint(path: str) -> "str | None":
    """sha256 of the rendered audio file, or None when it cannot be read."""
    digest = hashlib.sha256()
    try:
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def edits_document(editor: SpeakerEditor, audio_sha256: "str | None" = None) -> dict:
    return {"version": EDITS_VERSION, "audio_sha256": audio_sha256, "ops": copy.deepcopy(editor.ops)}


def save_edits(path: str, editor: SpeakerEditor, audio_sha256: "str | None" = None):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(edits_document(editor, audio_sha256), handle, indent=2, ensure_ascii=False)


def load_edits(path: str) -> "dict | None":
    """The saved document, or None when the file is missing, unreadable or from a newer plugin."""
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("ops"), list):
        return None
    if int(data.get("version", EDITS_VERSION) or EDITS_VERSION) > EDITS_VERSION:
        return None
    return data


def find_saved_edits(paths, audio_sha256: "str | None" = None) -> "tuple[str, dict] | tuple[None, None]":
    """The first saved edits among ``paths`` that are for this audio (or do not say which audio)."""
    for path in paths:
        if not path:
            continue
        data = load_edits(path)
        if data is None:
            continue
        saved_sha = data.get("audio_sha256")
        if saved_sha and audio_sha256 and saved_sha != audio_sha256:
            continue
        return path, data
    return None, None
