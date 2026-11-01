"""Re-check every speaker turn against every speaker's voice and move the ones that clearly sit in the wrong place.

Clustering decides a speaker per stretch of audio from windows that can straddle a change of voice, and
folding minor clusters away (merge_speakers) moves whole turns by an average. This pass looks at each turn
on its own: it is embedded, compared (cosine similarity) with every speaker's centroid, and moved to another
speaker only when that speaker beats the turn's current one by REASSIGN_MARGIN. Turns too short to embed
reliably, and turns whose best match is ambiguous, stay. Centroids are rebuilt from the new assignment and
the check repeated, at most MAX_ITERATIONS times.

The final centroids and each turn's best / second-best similarity are returned too, so later steps can derive
a per-cue speaker confidence and store voice centroids.

Works on the backends' raw (start, end, label) turns, like merge_speakers. Without an embedder (the pyannote
backend) it is a no-op.
"""

from __future__ import annotations

from collections.abc import Hashable
from dataclasses import dataclass, field

import numpy as np

from better_captions.engine.merge_speakers import EmbedFn, Raw, _centroids, _embed

# Another speaker must beat a turn's own by this much cosine similarity for the turn to move.
REASSIGN_MARGIN = 0.1
# Turns shorter than this are never moved (their embeddings are unreliable); they follow their cluster.
MIN_REASSIGN_SECONDS = 1.0
# Rounds of "move turns, rebuild centroids".
MAX_ITERATIONS = 2


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

    scores: list[TurnScore | None] = [None] * len(out)
    for i, vec in vecs.items():
        if not centroids:
            break
        ranked = sorted(((float(c @ vec), lb) for lb, c in centroids.items()), key=lambda x: -x[0])
        scores[i] = TurnScore(ranked[0][1], ranked[0][0], ranked[1][0] if len(ranked) > 1 else None)
    moved_total = sum(1 for before, after in zip(raw, out, strict=True) if before[2] != after[2])
    return ReassignResult(out, moved_total, centroids, scores)


def _label_centroids(raw: Raw, embed: EmbedFn) -> dict[Hashable, np.ndarray]:
    spans: dict[Hashable, list[tuple[float, float]]] = {}
    for s, e, label in raw:
        if e > s:
            spans.setdefault(label, []).append((float(s), float(e)))
    return _centroids(spans, embed)
