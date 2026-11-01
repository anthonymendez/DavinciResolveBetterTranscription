"""Turn segments into readable on-screen caption cues."""

from __future__ import annotations

from dataclasses import dataclass
from itertools import combinations, pairwise

from better_captions.schema import Cue, Segment, Transcript, Word

SENTENCE_END = (".", "!", "?", "…")
CLAUSE_END = (",", ";", ":", "—", "–")
CLOSERS = "\"'”’)]"


@dataclass
class CueOptions:
    max_chars_per_line: int = 42
    max_lines: int = 2
    max_duration: float = 6.0
    min_duration: float = 0.8
    min_gap: float = 0.05  # seconds between consecutive cues of the same speaker


def build_cues(transcript: Transcript, options: CueOptions | None = None) -> list[Cue]:
    """Split each segment at word boundaries into cues obeying options; sorted by start."""
    opts = options or CueOptions()
    cues: list[Cue] = []
    for segment in transcript.segments:
        for group in _segment_groups(segment, opts):
            cues.append(_make_cue(segment.speaker, group, opts))
    cues.sort(key=lambda c: (c.start, c.end))
    _extend_short_cues(cues, opts, transcript.duration)
    return cues


def _segment_groups(segment: Segment, opts: CueOptions) -> list[list[Word]]:
    """Greedily pack the segment's words into cues that fit the options."""
    groups: list[list[Word]] = []
    current: list[Word] = []
    for word in _words_of(segment):
        while current and not _fits([*current, word], opts):
            cut = _punct_cut(current)
            groups.append(current[:cut])
            current = current[cut:]
        current.append(word)
    if current:
        groups.append(current)
    return groups


def _words_of(segment: Segment) -> list[Word]:
    """Timed words for a segment; without word timings, spread the text evenly over its span."""
    timed = [w for w in segment.words if w.text.strip()]
    if timed:
        return timed
    tokens = segment.text.split()
    if not tokens:
        return []
    step = (segment.end - segment.start) / len(tokens)
    return [
        Word(t, segment.start + i * step, segment.start + (i + 1) * step, speaker=segment.speaker)
        for i, t in enumerate(tokens)
    ]


def _fits(words: list[Word], opts: CueOptions) -> bool:
    # A single word always fits on its own line, even if it is longer than a line or the duration.
    if len(words) == 1:
        return True
    if words[-1].end - words[0].start > opts.max_duration:
        return False
    return _split_lines(words, opts.max_lines, opts.max_chars_per_line) is not None


def _punct_cut(words: list[Word]) -> int:
    """Index to break a too-long run at: after the last sentence end, else the last clause end.

    Only breaks that keep at least a third of the run are considered, so we don't leave
    fragments. Returns len(words) when there is no good punctuation break.
    """
    n = len(words)
    for marks in (SENTENCE_END, CLAUSE_END):
        for k in range(n - 1, 0, -1):
            if k * 3 >= n and _ends_with(words[k - 1].text, marks):
                return k
    return n


def _split_lines(words: list[Word], max_lines: int, max_chars: int) -> list[list[str]] | None:
    """Split words into the fewest lines (<= max_lines) that fit max_chars, balanced and punctuation-aware.

    Returns None when no split fits. A group of one word is always accepted, so an over-long
    word is never broken mid-word.
    """
    texts = [w.text.strip() for w in words]
    n = len(texts)
    for lines in range(1, max_lines + 1):
        best: tuple[tuple[int, int], list[list[str]]] | None = None
        for cuts in combinations(range(1, n), lines - 1):
            bounds = (0, *cuts, n)
            groups = [texts[a:b] for a, b in pairwise(bounds)]
            widths = [len(" ".join(g)) for g in groups]
            if any(width > max_chars and len(g) > 1 for width, g in zip(widths, groups, strict=False)):
                continue
            # Minimise the longest line (balance), then prefer breaks after punctuation.
            punct = sum(_ends_with(texts[c - 1], SENTENCE_END + CLAUSE_END) for c in cuts)
            score = (max(widths), -punct)
            if best is None or score < best[0]:
                best = (score, groups)
        if best is not None:
            return best[1]
    return None


def _make_cue(speaker: str, words: list[Word], opts: CueOptions) -> Cue:
    lines = _split_lines(words, opts.max_lines, opts.max_chars_per_line)
    if lines is None:  # cannot happen for runs built by _fits; keep the text rather than drop it
        lines = [[w.text.strip() for w in words]]
    return Cue(
        speaker=speaker,
        start=words[0].start,
        end=words[-1].end,
        lines=[" ".join(line) for line in lines],
        speaker_confidence=_speaker_confidence(words),
    )


def _speaker_confidence(words: list[Word]) -> float | None:
    """The words' speaker_confidence weighted by duration (words without one are left out); None if none has one."""
    known = [(max(w.end - w.start, 0.01), w.speaker_confidence) for w in words if w.speaker_confidence is not None]
    if not known:
        return None
    return round(sum(d * c for d, c in known) / sum(d for d, _ in known), 3)


def _extend_short_cues(cues: list[Cue], opts: CueOptions, duration: float = 0.0) -> None:
    """Stretch cues shorter than min_duration, never reaching the next cue of the same speaker nor
    past the end of the audio (``duration``, when known); a cue ending after it is cut back to it."""
    next_start: dict[str, float] = {}
    for cue in reversed(cues):
        limit = next_start.get(cue.speaker)
        if cue.end - cue.start < opts.min_duration:
            target = cue.start + opts.min_duration
            if limit is not None:
                target = min(target, limit - opts.min_gap)
            cue.end = max(cue.end, target)
        if duration > 0 and cue.end > duration:
            cue.end = max(cue.start, duration)
        next_start[cue.speaker] = cue.start


def _ends_with(text: str, marks: tuple[str, ...]) -> bool:
    return text.strip().rstrip(CLOSERS).endswith(marks)
