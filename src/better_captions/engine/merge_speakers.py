"""Fold uncertain ("minor") speaker clusters into the most likely real speaker.

Threshold clustering over a long, many-voice recording tends to split off small clusters: a laugh, a few
words over someone else, a voice through a bad mic. On a ~6-person, 2.5 h recording that is 25+ speakers,
most of them with a handful of cues. After diarization a cluster with little total speech is treated as
minor, and each of its turns is reassigned to the major speaker whose voice is most similar (cosine
similarity of speaker embeddings against a centroid built from that speaker's longest turns). Without
embeddings a turn goes to the temporally nearest major speaker.

Works on the backends' raw (start, end, label) turns, before relabelling, so the final SPEAKER_NN labels
stay contiguous.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable
from dataclasses import dataclass

import numpy as np

Raw = list[tuple[float, float, Hashable]]
# (start, end) in seconds -> a speaker embedding for that stretch of audio, or None if it can't be computed.
EmbedFn = Callable[[float, float], "np.ndarray | None"]

# A cluster is minor when its total speech is under max(MINOR_SECONDS, MINOR_SHARE of all speech)...
MINOR_SECONDS = 15.0
MINOR_SHARE = 0.02
# ...but never when it holds this share of all speech or more: on a short clip 15 s can be a real speaker.
MAX_MINOR_SHARE = 0.05
# Centroids are built from each major speaker's longest turns, cropped around their middle.
CENTROID_TURNS = 8
MAX_EMBED_SECONDS = 10.0


@dataclass
class MergeResult:
    raw: Raw
    minor: int  # clusters folded away
    remaining: int  # clusters left
    by_embedding: int  # turns reassigned by voice similarity
    by_time: int  # turns reassigned to the temporally nearest major speaker


def minor_threshold(total_speech: float, min_seconds: float = MINOR_SECONDS, min_share: float = MINOR_SHARE) -> float:
    """Seconds of speech under which a cluster counts as minor."""
    return min(max(min_seconds, min_share * total_speech), MAX_MINOR_SHARE * total_speech)


def merge_minor_speakers(
    raw: Raw,
    embed: EmbedFn | None = None,
    *,
    min_seconds: float = MINOR_SECONDS,
    min_share: float = MINOR_SHARE,
    keep_at_least: int = 1,
) -> MergeResult:
    """Reassign the turns of minor clusters to major ones; at least `keep_at_least` clusters always remain.

    Minor clusters are folded smallest first, so when `keep_at_least` stops the merge it is the largest
    minor clusters that survive. Nothing happens when every cluster is minor (there is no real speaker to
    fold into) or none is.
    """
    turns = [(float(s), float(e), label) for s, e, label in raw if e > s]
    speech: dict[Hashable, float] = {}
    for s, e, label in turns:
        speech[label] = speech.get(label, 0.0) + (e - s)
    total = sum(speech.values())
    threshold = minor_threshold(total, min_seconds, min_share)
    candidates = sorted((lb for lb, sec in speech.items() if sec < threshold), key=lambda lb: (speech[lb], str(lb)))
    minor = candidates[: max(0, len(speech) - max(keep_at_least, 1))]
    if not minor or len(candidates) == len(speech):
        return MergeResult(list(raw), 0, len(speech), 0, 0)
    minor_set = set(minor)
    major = [label for label in speech if label not in minor_set]  # includes minor ones spared by keep_at_least

    by_label: dict[Hashable, list[tuple[float, float]]] = {}
    for s, e, label in turns:
        by_label.setdefault(label, []).append((s, e))

    centroids = _centroids({lb: by_label[lb] for lb in major}, embed) if embed else {}
    vecs: dict[int, np.ndarray | None] = {}  # index in raw -> embedding of that minor turn
    if centroids:
        for i, (s, e, label) in enumerate(raw):
            if label in minor_set and e > s:
                vecs[i] = _embed(embed, float(s), float(e))
    # A turn too short to embed falls back to the voice of its cluster as a whole.
    cluster_voice = {
        label: v
        for label in minor
        if (v := _unit_mean([vecs[i] for i in vecs if raw[i][2] == label and vecs[i] is not None])) is not None
    }

    out: Raw = []
    by_embedding = by_time = 0
    for i, (s, e, label) in enumerate(raw):
        if label not in minor_set:
            out.append((s, e, label))
            continue
        target = None
        vec = vecs.get(i)
        if vec is None:
            vec = cluster_voice.get(label)
        if vec is not None:
            target = max(centroids, key=lambda lb: float(centroids[lb] @ vec))
        if target is None:
            target = _nearest_in_time(float(s), float(e), major, by_label, speech)
            by_time += 1
        else:
            by_embedding += 1
        out.append((s, e, target))
    return MergeResult(out, len(minor), len(speech) - len(minor), by_embedding, by_time)


def _embed(embed: EmbedFn, start: float, end: float) -> np.ndarray | None:
    """Unit-length embedding of [start, end), cropped to its middle MAX_EMBED_SECONDS; None if unavailable."""
    if end - start > MAX_EMBED_SECONDS:
        mid = (start + end) / 2
        start, end = mid - MAX_EMBED_SECONDS / 2, mid + MAX_EMBED_SECONDS / 2
    vec = embed(start, end)
    if vec is None:
        return None
    vec = np.asarray(vec, dtype=np.float64).ravel()
    norm = float(np.linalg.norm(vec))
    return vec / norm if norm > 0 and np.isfinite(norm) else None


def _centroids(spans: dict[Hashable, list[tuple[float, float]]], embed: EmbedFn) -> dict[Hashable, np.ndarray]:
    """label -> unit-length mean embedding of its CENTROID_TURNS longest turns (labels with none are left out)."""
    centroids = {}
    for label, turns in spans.items():
        longest = sorted(turns, key=lambda t: t[0] - t[1])[:CENTROID_TURNS]
        centroid = _unit_mean([v for v in (_embed(embed, s, e) for s, e in longest) if v is not None])
        if centroid is not None:
            centroids[label] = centroid
    return centroids


def _unit_mean(vecs: list[np.ndarray]) -> np.ndarray | None:
    if not vecs:
        return None
    mean = np.mean(vecs, axis=0)
    norm = float(np.linalg.norm(mean))
    return mean / norm if norm > 0 else None


def _nearest_in_time(
    start: float,
    end: float,
    major: list[Hashable],
    by_label: dict[Hashable, list[tuple[float, float]]],
    speech: dict[Hashable, float],
) -> Hashable:
    """The major speaker with a turn closest to [start, end) (gap 0 if they overlap); ties go to the bigger one."""

    def gap(label):
        return min(max(s - end, start - e, 0.0) for s, e in by_label[label])

    return min(major, key=lambda lb: (gap(lb), -speech[lb], str(lb)))
