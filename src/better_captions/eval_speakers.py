"""Score a transcript's speaker labels against a corrected reference.

The reference is the user's corrected ``transcript.json`` (or the hypothesis with a saved
``edits.json`` applied, see ``reference_from_edits``). Two numbers come out:

- ``word_accuracy``: the share of reference words whose matched hypothesis word has the same
  speaker. Words are matched by time overlap, so timing drift between the two transcripts is fine.
- ``cue_fix_rate``: the share of hypothesis cues whose speaker would need fixing.

Speakers are compared by name, falling back to id. Diarization ids are arbitrary (``SPEAKER_00``
may be ``SPEAKER_01`` in another run), so each hypothesis speaker is first mapped one-to-one to
the reference speaker it overlaps most (Hungarian assignment); a hypothesis speaker left without
a partner is wrong everywhere it speaks.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

from better_captions.schema import Transcript


def _labels(transcript: Transcript, use_names: bool) -> dict[str, str]:
    """Speaker id -> comparison label (the name when it has one and ``use_names``, else the id)."""
    labels = {}
    for speaker in transcript.speakers:
        labels[speaker.id] = (speaker.name or speaker.id) if use_names else speaker.id
    return labels


def _words(transcript: Transcript, labels: dict[str, str]) -> list[tuple[float, float, str, str]]:
    """(start, end, normalised text, speaker label) per word, by start time. A word without its own
    speaker takes its segment's."""
    out = []
    for segment in transcript.segments:
        for word in segment.words:
            speaker = word.speaker or segment.speaker
            if not speaker:
                continue
            out.append((word.start, word.end, _normalise(word.text), labels.get(speaker, speaker)))
    out.sort(key=lambda w: (w[0], w[1]))
    return out


def _cues(transcript: Transcript, labels: dict[str, str]) -> list[tuple[float, float, str]]:
    return [(c.start, c.end, labels.get(c.speaker, c.speaker)) for c in transcript.cues]


def _normalise(text: str) -> str:
    return re.sub(r"[^\w]+", "", text.lower())


def _overlap(a_start: float, a_end: float, b_start: float, b_end: float) -> float:
    return min(a_end, b_end) - max(a_start, b_start)


def match_words(hyp: list[tuple], ref: list[tuple]) -> list[tuple[int, int]]:
    """One-to-one (hypothesis index, reference index) pairs of words that overlap in time.

    Candidates are taken best first: the same text before different text, then the longer overlap."""
    pairs = []
    first = 0  # hyp words are sorted by start; skip those that ended before the reference word
    for r_index, (r_start, r_end, r_text, _) in enumerate(ref):
        while first < len(hyp) and hyp[first][1] <= r_start and hyp[first][0] < r_start:
            first += 1
        for h_index in range(first, len(hyp)):
            h_start, h_end, h_text, _ = hyp[h_index]
            if h_start >= r_end:
                break
            overlap = _overlap(h_start, h_end, r_start, r_end)
            if overlap > 0:
                pairs.append((h_text == r_text, overlap, h_index, r_index))
    pairs.sort(key=lambda p: (not p[0], -p[1], p[2], p[3]))
    used_hyp: set[int] = set()
    used_ref: set[int] = set()
    matched = []
    for _same, _overlap_s, h_index, r_index in pairs:
        if h_index in used_hyp or r_index in used_ref:
            continue
        used_hyp.add(h_index)
        used_ref.add(r_index)
        matched.append((h_index, r_index))
    return sorted(matched, key=lambda m: m[1])


def assign(weights: list[list[float]]) -> list[int]:
    """Hungarian algorithm: for each row the column of the assignment maximising the total weight,
    or -1 when the row gets none (more rows than columns). Rows and columns may differ in number."""
    rows = len(weights)
    cols = len(weights[0]) if rows else 0
    size = max(rows, cols)
    if size == 0:
        return []
    top = max((max(row) for row in weights if row), default=0.0)
    # Square cost matrix to minimise; padding cells cost the same as a zero-weight pairing.
    cost = [[top - (weights[i][j] if i < rows and j < cols else 0.0) for j in range(size)] for i in range(size)]
    inf = float("inf")
    u = [0.0] * (size + 1)
    v = [0.0] * (size + 1)
    owner = [0] * (size + 1)  # owner[j]: the 1-based row assigned to column j
    way = [0] * (size + 1)
    for i in range(1, size + 1):
        owner[0] = i
        j0 = 0
        minv = [inf] * (size + 1)
        used = [False] * (size + 1)
        while True:
            used[j0] = True
            i0, delta, j1 = owner[j0], inf, 0
            for j in range(1, size + 1):
                if used[j]:
                    continue
                cur = cost[i0 - 1][j - 1] - u[i0] - v[j]
                if cur < minv[j]:
                    minv[j], way[j] = cur, j0
                if minv[j] < delta:
                    delta, j1 = minv[j], j
            for j in range(size + 1):
                if used[j]:
                    u[owner[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if owner[j0] == 0:
                break
        while j0:
            j1 = way[j0]
            owner[j0] = owner[j1]
            j0 = j1
    result = [-1] * rows
    for j in range(1, size + 1):
        row = owner[j] - 1
        if row < rows and j - 1 < cols:
            result[row] = j - 1
    return result


def map_speakers(counts: dict[tuple[str, str], float]) -> dict[str, str]:
    """Hypothesis label -> reference label, one-to-one, maximising the summed co-occurrence ``counts``
    (keyed ``(hypothesis label, reference label)``). Pairs that never co-occur are left out."""
    hyp_labels = sorted({h for h, _ in counts})
    ref_labels = sorted({r for _, r in counts})
    weights = [[counts.get((h, r), 0.0) for r in ref_labels] for h in hyp_labels]
    mapping = {}
    for h_index, r_index in enumerate(assign(weights)):
        if r_index >= 0 and weights[h_index][r_index] > 0:
            mapping[hyp_labels[h_index]] = ref_labels[r_index]
    return mapping


def _cue_votes(hyp_cues, ref_cues) -> dict[int, dict[str, float]]:
    """Per hypothesis cue: seconds it overlaps each reference speaker's cues."""
    votes: dict[int, dict[str, float]] = {}
    for h_index, (h_start, h_end, _) in enumerate(hyp_cues):
        for r_start, r_end, r_label in ref_cues:
            overlap = _overlap(h_start, h_end, r_start, r_end)
            if overlap > 0:
                bucket = votes.setdefault(h_index, {})
                bucket[r_label] = bucket.get(r_label, 0.0) + overlap
    return votes


def evaluate(hypothesis: Transcript | dict[str, Any], reference: Transcript | dict[str, Any]) -> dict[str, Any]:
    """Compare ``hypothesis`` speaker labels with ``reference`` (transcripts or their JSON dicts)."""
    hyp = hypothesis if isinstance(hypothesis, Transcript) else Transcript.from_dict(hypothesis)
    ref = reference if isinstance(reference, Transcript) else Transcript.from_dict(reference)
    # Hypothesis labels are ids only: its names are guesses, and the mapping lines it up anyway.
    hyp_labels = _labels(hyp, use_names=False)
    ref_labels = _labels(ref, use_names=True)
    hyp_words = _words(hyp, hyp_labels)
    ref_words = _words(ref, ref_labels)
    hyp_cues = _cues(hyp, hyp_labels)
    ref_cues = _cues(ref, ref_labels)

    matches = match_words(hyp_words, ref_words)
    votes = _cue_votes(hyp_cues, ref_cues)

    # What each hypothesis speaker overlaps: matched words, or cue seconds in a transcript without words.
    counts: dict[tuple[str, str], float] = {}
    if matches:
        for h_index, r_index in matches:
            key = (hyp_words[h_index][3], ref_words[r_index][3])
            counts[key] = counts.get(key, 0.0) + 1.0
    else:
        for h_index, bucket in votes.items():
            for r_label, seconds in bucket.items():
                key = (hyp_cues[h_index][2], r_label)
                counts[key] = counts.get(key, 0.0) + seconds
    mapping = map_speakers(counts)

    matched_ref = {r_index: h_index for h_index, r_index in matches}
    correct = sum(
        1 for r_index, h_index in matched_ref.items() if mapping.get(hyp_words[h_index][3]) == ref_words[r_index][3]
    )

    compared = fixes = 0
    wrong_cues = []
    for h_index, (start, end, label) in enumerate(hyp_cues):
        bucket = votes.get(h_index)
        if not bucket:
            continue  # nothing to compare with: not a speaker mistake
        compared += 1
        wanted = max(sorted(bucket), key=lambda name: bucket[name])
        if mapping.get(label) != wanted:
            fixes += 1
            wrong_cues.append({"index": h_index, "start": start, "end": end, "speaker": label, "expected": wanted})

    return {
        "words_reference": len(ref_words),
        "words_matched": len(matched_ref),
        "words_correct": correct,
        "word_accuracy": correct / len(ref_words) if ref_words else 1.0,
        "cues_hypothesis": len(hyp_cues),
        "cues_compared": compared,
        "cues_needing_fix": fixes,
        "cue_fix_rate": fixes / compared if compared else 0.0,
        "speaker_mapping": mapping,
        "unmapped_speakers": sorted(({cue[2] for cue in hyp_cues} | {word[3] for word in hyp_words}) - set(mapping)),
        "cues_to_fix": wrong_cues,
    }


def reference_from_edits(hypothesis: dict[str, Any], edits_path: str | Path) -> tuple[dict[str, Any], int]:
    """The hypothesis transcript with the saved ``edits.json`` applied, and how many edits did not fit.

    The edit logic lives in the Resolve plugin (stdlib only), which sits beside the engine in a checkout."""
    try:
        from better_captions_plugin import speaker_edits
    except ImportError:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "resolve_plugin" / "src"))
        try:
            from better_captions_plugin import speaker_edits
        except ImportError as exc:
            raise SystemExit("applying edits needs the Resolve plugin sources (resolve_plugin/src): %s" % exc) from exc
    document = speaker_edits.load_edits(str(edits_path))
    if document is None:
        raise ValueError("%s is not a readable edits file" % edits_path)
    editor = speaker_edits.SpeakerEditor(hypothesis)
    editor.reapply(document["ops"])
    return editor.data, len(editor.skipped)


def format_report(report: dict[str, Any]) -> str:
    def pct(value: float) -> str:
        return "%.1f%%" % (100.0 * value)

    lines = [
        "words:  %s of %d reference words have the right speaker (%d matched by time)"
        % (pct(report["word_accuracy"]), report["words_reference"], report["words_matched"]),
        "cues:   %d of %d need a speaker fix (%s)"
        % (report["cues_needing_fix"], report["cues_compared"], pct(report["cue_fix_rate"])),
    ]
    if report["speaker_mapping"]:
        pairs = ", ".join("%s -> %s" % item for item in sorted(report["speaker_mapping"].items()))
        lines.append("speakers: " + pairs)
    if report["unmapped_speakers"]:
        lines.append("no reference speaker for: " + ", ".join(report["unmapped_speakers"]))
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Score a transcript's speaker labels against a corrected one.")
    parser.add_argument("hypothesis", help="transcript.json from the engine")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--reference", help="the corrected transcript.json")
    source.add_argument(
        "--edits", help="edits.json saved by the plugin; the reference is the hypothesis with them applied"
    )
    parser.add_argument("--json", action="store_true", help="print the full report as JSON")
    args = parser.parse_args(argv)

    hypothesis = json.loads(Path(args.hypothesis).read_text(encoding="utf-8"))
    skipped = 0
    if args.edits:
        reference, skipped = reference_from_edits(hypothesis, args.edits)
    else:
        reference = json.loads(Path(args.reference).read_text(encoding="utf-8"))
    report = evaluate(hypothesis, reference)
    if args.edits:
        report["edits_skipped"] = skipped
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
    else:
        print(format_report(report))
        if skipped:
            print("%d saved edit(s) no longer fit and were skipped" % skipped)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
