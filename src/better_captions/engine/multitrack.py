"""Transcribe each audio track on its own instead of a mix of them (``--track ROLE:NAME=PATH``).

Every track has a role:

* ``person``: one named person talks on it (a microphone). Its words are that person's, with
  certainty: they get a pinned speaker (``fixed_tracks.speaker_ids``: ``PINNED_00``...) named by the user.
* ``group``: several people share it (a Discord call). Only these tracks are diarized, each on its own,
  and their speakers are numbered ``SPEAKER_00``... across all group tracks.
* ``none``: not speech (game audio, music). It is not transcribed at all.

``pipeline.run`` transcribes and diarizes the tracks; this module holds the pure parts: parsing the
specs, numbering the group speakers, dropping *bleed* and grouping the merged words into segments.

Bleed is one person's speech picked up by another track (a microphone hearing the Discord call in the
speakers, or the reverse). Whisper then writes the same word, at the same moment, on both tracks.
``drop_bleed`` pairs such words up and keeps the one on the track where it is louder; when the levels are
too close to tell (or unknown), a person track wins over a group track. Pairs close together in time are
decided together, so a phrase isn't split between two tracks word by word.

Words of different speakers may overlap in time (two people talking at once): both are kept, and
``group_segments`` groups each speaker's words on their own before sorting the segments by time.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass, replace

import numpy as np

from better_captions.engine import align, fixed_tracks
from better_captions.schema import Segment, Speaker, SpeakerTurn, Word

PERSON = "person"
GROUP = "group"
NONE = "none"
ROLES = (PERSON, GROUP, NONE)
_ROLE_ALIASES = {"not-speech": NONE, "nospeech": NONE, "people": GROUP}

GROUP_ID_FORMAT = "SPEAKER_%02d"
BLEED_TOLERANCE = 0.35  # s: two words this close (or overlapping) count as "at the same time"
BLEED_MARGIN_DB = 3.0  # a word must be this much louder on one track to win on loudness alone
BLEED_RUN_GAP = 1.0  # s: bleed pairs closer than this (between the same two tracks) are decided together
SILENCE_DB = -120.0

# (track position, start, end) -> level in dB, or None when it can't be measured
LevelFn = Callable[[int, float, float], "float | None"]


@dataclass(frozen=True)
class Track:
    """One ``--track ROLE:NAME=PATH``. ``name`` is the person for ``person``, a label otherwise (may be "")."""

    role: str
    name: str
    path: str

    @property
    def speech(self) -> bool:
        return self.role != NONE


def parse_track(spec: str) -> Track:
    """``"person:Anthony=mic.wav"``, ``"group:Discord=d.wav"``, ``"group=d.wav"``, ``"none:Game=g.wav"``,
    or ``"Anthony=mic.wav"`` (a person, as ``--fixed-track``). Split at the first ``=``, so the path may
    contain more; the role is what comes before the first ``:``."""
    left, sep, path = str(spec).partition("=")
    path = path.strip()
    role, colon, name = left.partition(":")
    role = role.strip().lower()
    role = _ROLE_ALIASES.get(role, role)
    if not colon:
        if role in (GROUP, NONE):
            name = ""
        else:  # NAME=PATH
            role, name = PERSON, left
    name = name.strip()
    if not sep or not path:
        raise ValueError(f"expected ROLE:NAME=PATH, got {spec!r}")
    if role not in ROLES:
        raise ValueError(f"unknown track role {role!r} in {spec!r} (expected one of {', '.join(ROLES)})")
    if role == PERSON and not name:
        raise ValueError(f"a person track needs the person's name: {spec!r}")
    return Track(role=role, name=name, path=path)


def from_fixed(track: fixed_tracks.FixedTrack) -> Track:
    """``--fixed-track NAME=PATH`` as a person track."""
    return Track(role=PERSON, name=track.name, path=track.path)


def _as_fixed(tracks: list[Track]) -> list[fixed_tracks.FixedTrack]:
    return [fixed_tracks.FixedTrack(t.name, t.path) for t in tracks if t.role == PERSON]


def person_ids(tracks: list[Track]) -> dict[str, str]:
    """Person name -> pinned speaker id; person tracks that share a name share a speaker."""
    return fixed_tracks.speaker_ids(_as_fixed(tracks))


def person_speakers(tracks: list[Track]) -> dict[str, Speaker]:
    """Pinned speaker id -> the named Speaker (``name_source`` "user")."""
    return fixed_tracks.fixed_speakers(_as_fixed(tracks))


def describe(tracks: list[Track]) -> list[dict]:
    """``transcript.meta["tracks"]``: each track's role, name, audio and (person tracks) speaker id."""
    ids = person_ids(tracks)
    out = []
    for t in tracks:
        entry = {"role": t.role, "name": t.name, "audio": t.path}
        if t.role == PERSON:
            entry["id"] = ids[t.name]
        out.append(entry)
    return out


# --- speakers ----------------------------------------------------------------


class GroupNumbering:
    """Numbers the speakers found on each group track ``SPEAKER_00``, ``SPEAKER_01``... across all of them,
    so two group tracks never share an id."""

    def __init__(self):
        self.count = 0

    def _next(self) -> str:
        sid = GROUP_ID_FORMAT % self.count
        self.count += 1
        return sid

    def assign(self, words: list[Word], turns: list[SpeakerTurn]) -> list[Word]:
        """``words`` of one group track labelled from that track's ``turns`` (``align.assign_speakers``), with
        the track's own labels renamed to fresh global ids; one fresh id for all of them if there are no turns."""
        if not words:
            return []
        if not turns:
            sid = self._next()
            return [replace(w, speaker=sid) for w in words]
        assigned = align.assign_speakers(words, turns)
        # numbered in order of first appearance among the words, so no id is spent on a cluster with no words
        mapping = {label: self._next() for label in dict.fromkeys(w.speaker for w in assigned)}
        return [replace(w, speaker=mapping[w.speaker]) for w in assigned]


def label_person(words: list[Word], speaker_id: str) -> list[Word]:
    return [replace(w, speaker=speaker_id) for w in words]


# --- bleed -------------------------------------------------------------------

_PUNCT = re.compile(r"[^\w']+", re.UNICODE)


def normalize(text: str) -> str:
    """Lower case, without punctuation: the text two transcriptions of the same word share."""
    return _PUNCT.sub("", text.strip().lower()).strip("'")


def _close(a: Word, b: Word) -> bool:
    overlap = min(a.end, b.end) - max(a.start, b.start)
    return overlap > 0 or abs(a.start - b.start) <= BLEED_TOLERANCE


def find_bleed_pairs(words: list[tuple[int, Word]]) -> list[tuple[int, int]]:
    """Pairs ``(i, j)`` of positions in ``words`` (``(track position, word)``) that are the same word heard on
    two tracks at the same moment. Each word is in at most one pair; the closest match in time is taken."""
    order = sorted(range(len(words)), key=lambda k: words[k][1].start)
    norms = [normalize(w.text) for _t, w in words]
    paired: set[int] = set()
    pairs = []
    for pos, i in enumerate(order):
        if i in paired or not norms[i]:
            continue
        ti, wi = words[i]
        best, best_gap = None, None
        for j in order[pos + 1 :]:
            tj, wj = words[j]
            if wj.start - wi.start > BLEED_TOLERANCE and wj.start >= wi.end:
                break
            if j in paired or tj == ti or norms[j] != norms[i] or not _close(wi, wj):
                continue
            gap = abs(wj.start - wi.start)
            if best_gap is None or gap < best_gap:
                best, best_gap = j, gap
        if best is not None:
            paired.update((i, best))
            pairs.append((i, best))
    return pairs


def drop_bleed(
    words: list[tuple[int, Word]], roles: list[str], level: LevelFn | None = None
) -> tuple[list[tuple[int, Word]], int]:
    """``words`` (``(track position, word)``) without the copies heard on a second track; returns them and how
    many were dropped. ``roles[track position]`` is that track's role; ``level`` measures a word on a track."""
    pairs = find_bleed_pairs(words)
    if not pairs:
        return list(words), 0
    # orient each pair (lower track position first) and decide runs of pairs between the same two tracks together
    oriented = []
    for i, j in pairs:
        if words[i][0] > words[j][0]:
            i, j = j, i
        oriented.append((i, j))
    oriented.sort(key=lambda p: min(words[p[0]][1].start, words[p[1]][1].start))
    runs: list[list[tuple[int, int]]] = []
    last_end: dict[tuple[int, int], float] = {}
    open_run: dict[tuple[int, int], list[tuple[int, int]]] = {}
    for i, j in oriented:
        key = (words[i][0], words[j][0])
        start = min(words[i][1].start, words[j][1].start)
        if key in open_run and start - last_end[key] <= BLEED_RUN_GAP:
            open_run[key].append((i, j))
        else:
            open_run[key] = [(i, j)]
            runs.append(open_run[key])
        last_end[key] = max(words[i][1].end, words[j][1].end)
    drop: set[int] = set()
    for run in runs:
        keep_first = _first_wins(run, words, roles, level)
        drop.update(j if keep_first else i for i, j in run)
    return [w for k, w in enumerate(words) if k not in drop], len(drop)


def _first_wins(run, words, roles, level) -> bool:
    """For a run of bleed pairs ``(i, j)`` between the same two tracks: does the first track keep them?"""
    track_a, track_b = words[run[0][0]][0], words[run[0][1]][0]
    diffs = []
    if level is not None:
        for i, j in run:
            a = level(track_a, words[i][1].start, words[i][1].end)
            b = level(track_b, words[j][1].start, words[j][1].end)
            if a is not None and b is not None:
                diffs.append(a - b)
    if diffs:
        mean = sum(diffs) / len(diffs)
        if mean >= BLEED_MARGIN_DB:
            return True
        if mean <= -BLEED_MARGIN_DB:
            return False
    person_a, person_b = roles[track_a] == PERSON, roles[track_b] == PERSON
    if person_a != person_b:
        return person_a
    if diffs:
        return sum(diffs) >= 0
    prob_a = sum(words[i][1].probability for i, _j in run)
    prob_b = sum(words[j][1].probability for _i, j in run)
    return prob_a >= prob_b


class TrackLevels:
    """``LevelFn`` over audio files: the RMS level (dB) of a span of a track, read by seeking (soundfile), so
    long renders are never loaded whole. A track it can't read measures None."""

    def __init__(self, paths: list[str]):
        self.paths = list(paths)
        self._files: dict[int, object] = {}

    def _file(self, track: int):
        if track not in self._files:
            try:
                import soundfile as sf

                self._files[track] = sf.SoundFile(self.paths[track])
            except Exception:
                self._files[track] = None
        return self._files[track]

    def __call__(self, track: int, start: float, end: float) -> float | None:
        handle = self._file(track)
        if handle is None:
            return None
        try:
            rate = handle.samplerate
            first = max(0, int(start * rate))
            count = max(1, int((end - start) * rate))
            if first >= handle.frames:
                return SILENCE_DB
            handle.seek(first)
            data = handle.read(count, dtype="float32", always_2d=True)
        except Exception:
            return None
        if not len(data):
            return SILENCE_DB
        mono = np.asarray(data, dtype=np.float64).mean(axis=1)
        mean_square = float(np.mean(mono * mono))
        return SILENCE_DB if mean_square <= 1e-12 else 10.0 * math.log10(mean_square)

    def close(self) -> None:
        for handle in self._files.values():
            if handle is not None:
                try:
                    handle.close()
                except Exception:
                    pass
        self._files.clear()


# --- segments ----------------------------------------------------------------


def merge_words(words: list[tuple[int, Word]]) -> list[Word]:
    """All tracks' words in time order (overlapping speech of different tracks is kept)."""
    return [w for _t, w in sorted(words, key=lambda tw: (tw[1].start, tw[1].end, tw[0]))]


def group_segments(words: list[Word]) -> list[Segment]:
    """Segments of each speaker's own words (so a speaker talking over another doesn't cut their sentences
    into pieces), sorted by start."""
    by_speaker: dict[str, list[Word]] = {}
    for w in words:
        by_speaker.setdefault(w.speaker or align.DEFAULT_SPEAKER, []).append(w)
    segments = []
    for speaker_words in by_speaker.values():
        segments.extend(align.group_segments(sorted(speaker_words, key=lambda w: (w.start, w.end))))
    segments.sort(key=lambda s: (s.start, s.end))
    return segments
