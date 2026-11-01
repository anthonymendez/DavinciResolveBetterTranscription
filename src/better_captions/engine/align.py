from __future__ import annotations

import bisect
from dataclasses import replace

from better_captions.schema import Segment, SpeakerTurn, Word

DEFAULT_SPEAKER = "SPEAKER_00"

_SENTENCE_END = (".", "?", "!", "…", "。", "？", "！")
_TRAILING_CLOSERS = "\"'”’)]}»"
# Lower-cased words ending in "." that rarely end a sentence.
_ABBREVIATIONS = {
    "mr.",
    "mrs.",
    "ms.",
    "dr.",
    "prof.",
    "st.",
    "jr.",
    "sr.",
    "vs.",
    "etc.",
    "e.g.",
    "i.e.",
    "a.m.",
    "p.m.",
}


def assign_speakers(words: list[Word], turns: list[SpeakerTurn]) -> list[Word]:
    """Set Word.speaker (and speaker_confidence, the turn's confidence) from the turn with the greatest overlap
    (nearest turn if none overlaps)."""
    if not turns:
        return [replace(w, speaker=DEFAULT_SPEAKER) for w in words]

    turns = sorted(turns, key=lambda t: (t.start, t.end))
    starts = [t.start for t in turns]
    longest = max(t.end - t.start for t in turns)
    # latest_end[i]: index of the turn ending last among turns[0..i].
    latest_end: list[int] = []
    for i, t in enumerate(turns):
        latest_end.append(i if not latest_end or t.end > turns[latest_end[-1]].end else latest_end[-1])

    out = []
    for w in words:
        # Turns that can overlap w start before w.end (or at w.start for a zero-length word)
        # and, being at most `longest` long, no earlier than w.start - longest.
        hi = bisect.bisect_right(starts, w.start) if w.end <= w.start else bisect.bisect_left(starts, w.end)
        lo = bisect.bisect_left(starts, w.start - longest)
        best, best_overlap = None, 0.0
        for t in turns[lo:hi]:
            overlap = _overlap(w, t)
            if overlap > best_overlap:
                best, best_overlap = t, overlap
        if best is None:
            best = _nearest(w, turns, latest_end, hi)
        out.append(replace(w, speaker=best.speaker, speaker_confidence=best.confidence))
    return out


def _overlap(w: Word, t: SpeakerTurn) -> float:
    if w.end <= w.start:  # zero-length word: count it as overlapping the turn containing it
        return 1e-9 if t.start <= w.start < t.end else 0.0
    return min(w.end, t.end) - max(w.start, t.start)


def _nearest(w: Word, turns: list[SpeakerTurn], latest_end: list[int], hi: int) -> SpeakerTurn:
    """Closest turn to a word overlapping none: the latest-ending turn before it or the first one after."""
    before = turns[latest_end[hi - 1]] if hi > 0 else None
    after = turns[hi] if hi < len(turns) else None
    if before is None:
        return after
    if after is None:
        return before
    return before if w.start - before.end <= after.start - w.end else after


def group_segments(words: list[Word], max_gap: float = 1.0) -> list[Segment]:
    """Group consecutive same-speaker words into sentence-like segments."""
    segments: list[Segment] = []
    current: list[Word] = []
    for w in words:
        if current and (
            _speaker(w) != _speaker(current[-1])
            or w.start - current[-1].end > max_gap
            or _ends_sentence(current[-1].text)
        ):
            segments.append(_make_segment(current))
            current = []
        current.append(w)
    if current:
        segments.append(_make_segment(current))
    return segments


def _speaker(w: Word) -> str:
    return w.speaker or DEFAULT_SPEAKER


def _ends_sentence(text: str) -> bool:
    stripped = text.strip().rstrip(_TRAILING_CLOSERS)
    return stripped.endswith(_SENTENCE_END) and stripped.lower() not in _ABBREVIATIONS


def _make_segment(words: list[Word]) -> Segment:
    return Segment(
        speaker=_speaker(words[0]),
        start=words[0].start,
        end=max(w.end for w in words),
        text=_join([w.text for w in words]),
        words=list(words),
    )


def _join(tokens: list[str]) -> str:
    """Join word texts with spaces, except between CJK characters (written without spaces)."""
    text = ""
    for token in (t.strip() for t in tokens):
        if not token:
            continue
        if text and not (_is_cjk(text[-1]) and _is_cjk(token[0])):
            text += " "
        text += token
    return text


def _is_cjk(ch: str) -> bool:
    cp = ord(ch)
    return (
        0x3040 <= cp <= 0x30FF  # hiragana, katakana
        or 0x3400 <= cp <= 0x4DBF
        or 0x4E00 <= cp <= 0x9FFF
        or 0xF900 <= cp <= 0xFAFF
        or 0x3000 <= cp <= 0x303F  # CJK punctuation
        or 0xFF00 <= cp <= 0xFFEF  # full-width forms
    )
