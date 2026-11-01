"""Re-check every speaker turn against every speaker's voice and move the ones that clearly sit in the wrong place.

Clustering decides a speaker per stretch of audio from windows that can straddle a change of voice, and
folding minor clusters away (merge_speakers) moves whole turns by an average. This pass looks at each turn
on its own: it is embedded, compared (cosine similarity) with every speaker's centroid, and moved to another
speaker only when that speaker beats the turn's current one by REASSIGN_MARGIN. Turns too short to embed
reliably, and turns whose best match is ambiguous, stay. Centroids are rebuilt from the new assignment and
the check repeated, at most MAX_ITERATIONS times.

The final centroids and each turn's best / second-best similarity are returned too, so later steps can derive
a per-cue speaker confidence and store voice centroids. For those scores a turn is compared with its own
speaker's centroid rebuilt without it (leave-one-out): a turn that helped build the centroid would otherwise
partly match itself, which lifts a doubtful turn's confidence most on a speaker with only a few turns. A
speaker whose centroid is that one turn alone keeps the full centroid.

Works on the backends' raw (start, end, label) turns, like merge_speakers. Without an embedder (the pyannote
backend) it is a no-op.
"""

from __future__ import annotations

from collections.abc import Hashable
from dataclasses import dataclass, field

import numpy as np

from better_captions.engine.merge_speakers import CENTROID_TURNS, EmbedFn, Raw, _centroids, _embed, _unit_mean

# Another speaker must beat a turn's own by this much cosine similarity for the turn to move.
REASSIGN_MARGIN = 0.1
# Turns shorter than this are never moved (their embeddings are unreliable); they follow their cluster.
MIN_REASSIGN_SECONDS = 1.0
# Rounds of "move turns, rebuild centroids".
MAX_ITERATIONS = 2
# A turn whose own speaker beats the next best by this much cosine similarity has confidence 1.
CONFIDENT_MARGIN = 0.2


@dataclass
class TurnScore:
    """How well one turn matches the speakers' voices."""

    label: Hashable  # the speaker whose centroid it is most similar to (not necessarily where it sits)
    best: float  # cosine similarity to that centroid
    second: float | None  # similarity to the next best speaker; None when there is only one speaker


@dataclass
class ReassignResult:
    raw: Raw
    moved: int  # turns given to another speaker
    centroids: dict[Hashable, np.ndarray] = field(default_factory=dict)  # label -> unit-length voice centroid
    scores: list[TurnScore | None] = field(default_factory=list)  # aligned with raw; None = turn not embedded


def reassign_segments(
    raw: Raw,
    embed: EmbedFn | None = None,
    *,
    margin: float = REASSIGN_MARGIN,
    min_seconds: float = MIN_REASSIGN_SECONDS,
    max_iterations: int = MAX_ITERATIONS,
) -> ReassignResult:
    """Move turns whose voice matches another speaker clearly better than their own; see the module docs."""
    out: Raw = [(s, e, label) for s, e, label in raw]
    if embed is None:
        return ReassignResult(out, 0, {}, [None] * len(out))

    cache: dict[tuple[float, float], np.ndarray | None] = {}

    def cached(start: float, end: float):
        if (start, end) not in cache:
            cache[(start, end)] = embed(start, end)
        return cache[(start, end)]

    vecs: dict[int, np.ndarray] = {}  # index -> embedding, for the turns long enough to compare
    for i, (s, e, _) in enumerate(out):
        if e - s >= min_seconds and e > s and (v := _embed(cached, float(s), float(e))) is not None:
            vecs[i] = v

    centroids = _label_centroids(out, cached)
    for _ in range(max(0, max_iterations)):
        if len(centroids) < 2:
            break
        counts: dict[Hashable, int] = {}
        for _, _, label in out:
            counts[label] = counts.get(label, 0) + 1
        moved = 0
        for i, vec in vecs.items():
            label = out[i][2]
            if label not in centroids or counts[label] <= 1:  # never empty a speaker out
                continue
            sims = {lb: float(c @ vec) for lb, c in centroids.items()}
            best = max(sims, key=lambda lb: (sims[lb], lb == label))
            if best != label and sims[best] - sims[label] >= margin:
                out[i] = (out[i][0], out[i][1], best)
                counts[label] -= 1
                counts[best] = counts.get(best, 0) + 1
                moved += 1
        if not moved:
            break
        centroids = _label_centroids(out, cached)

    members = _centroid_members(out, cached)
    centroids = {lb: c for lb, m in members.items() if (c := _unit_mean(list(m.values()))) is not None}
    scores: list[TurnScore | None] = [None] * len(out)
    for i, vec in vecs.items():
        if not centroids:
            break
        candidates = dict(centroids)
        label = out[i][2]
        if i in members.get(label, {}):
            # Leave the turn out of its own speaker's centroid, or it partly matches itself.
            rest = _unit_mean([v for j, v in members[label].items() if j != i])
            if rest is not None:
                candidates[label] = rest
        ranked = sorted(((float(c @ vec), lb) for lb, c in candidates.items()), key=lambda x: -x[0])
        scores[i] = TurnScore(ranked[0][1], ranked[0][0], ranked[1][0] if len(ranked) > 1 else None)
    moved_total = sum(1 for before, after in zip(raw, out, strict=True) if before[2] != after[2])
    return ReassignResult(out, moved_total, centroids, scores)


def turn_confidence(score: TurnScore | None, label: Hashable, margin: float = CONFIDENT_MARGIN) -> float | None:
    """0..1: how clearly a turn sitting with ``label`` sounds like that speaker rather than the next best.
    0 when another speaker's voice matches it better (it stayed only because the lead was below the margin);
    None when the turn was not embedded."""
    if score is None:
        return None
    if score.label != label:
        return 0.0
    if score.second is None:
        return 1.0
    return round(min(1.0, max(0.0, (score.best - score.second) / margin)), 3)


def _label_centroids(raw: Raw, embed: EmbedFn) -> dict[Hashable, np.ndarray]:
    spans: dict[Hashable, list[tuple[float, float]]] = {}
    for s, e, label in raw:
        if e > s:
            spans.setdefault(label, []).append((float(s), float(e)))
    return _centroids(spans, embed)


def _centroid_members(raw: Raw, embed: EmbedFn) -> dict[Hashable, dict[int, np.ndarray]]:
    """label -> {index in raw: embedding} of the turns its centroid is built from; the same turns as
    _centroids picks, so the unit mean of each label's embeddings is that label's centroid."""
    by_label: dict[Hashable, list[int]] = {}
    for i, (s, e, label) in enumerate(raw):
        if e > s:
            by_label.setdefault(label, []).append(i)
    members: dict[Hashable, dict[int, np.ndarray]] = {}
    for label, indices in by_label.items():
        longest = sorted(indices, key=lambda i: float(raw[i][0]) - float(raw[i][1]))[:CENTROID_TURNS]
        vecs = {i: v for i in longest if (v := _embed(embed, float(raw[i][0]), float(raw[i][1]))) is not None}
        if vecs:
            members[label] = vecs
    return members
