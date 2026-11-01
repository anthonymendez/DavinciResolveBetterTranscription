"""Speaker-label scoring against a corrected reference transcript."""

import copy
import json
import subprocess
import sys
from pathlib import Path

import pytest

from better_captions import eval_speakers
from better_captions.eval_speakers import assign, evaluate, map_speakers

REPO = Path(__file__).resolve().parent.parent

# (speaker, words) per cue; every word lasts 0.4 s with 0.1 s gaps, cues are 2 s apart.
CUES = [
    ("SPEAKER_00", "hey there how are you"),
    ("SPEAKER_01", "good thanks and you"),
    ("SPEAKER_00", "ready for the race"),
    ("SPEAKER_02", "lets go then"),
]


def make(names=None):
    segments, cues = [], []
    for index, (speaker, text) in enumerate(CUES):
        start = index * 5.0
        words = []
        for n, token in enumerate(text.split()):
            words.append(
                {
                    "text": token,
                    "start": start + n * 0.5,
                    "end": start + n * 0.5 + 0.4,
                    "probability": 1.0,
                    "speaker": speaker,
                }
            )
        end = words[-1]["end"]
        segments.append({"speaker": speaker, "start": start, "end": end, "text": text, "words": words})
        cues.append({"speaker": speaker, "start": start, "end": end, "lines": [text]})
    names = names or {}
    return {
        "language": "en",
        "duration": 20.0,
        "speakers": [
            {"id": sid, "name": names.get(sid), "name_confidence": 0.0, "name_source": None}
            for sid in ("SPEAKER_00", "SPEAKER_01", "SPEAKER_02")
        ],
        "segments": segments,
        "cues": cues,
        "meta": {},
        "schema_version": 1,
    }


def relabel(data, mapping):
    """The same transcript with speaker ids renamed (diarization numbers speakers arbitrarily)."""
    data = copy.deepcopy(data)
    for speaker in data["speakers"]:
        speaker["id"] = mapping[speaker["id"]]
    for cue in data["cues"]:
        cue["speaker"] = mapping[cue["speaker"]]
    for segment in data["segments"]:
        segment["speaker"] = mapping[segment["speaker"]]
        for word in segment["words"]:
            word["speaker"] = mapping[word["speaker"]]
    return data


def test_identical_transcripts_score_100_percent():
    report = evaluate(make(), make())
    assert report["word_accuracy"] == 1.0
    assert report["words_reference"] == 16
    assert report["words_matched"] == 16
    assert report["cue_fix_rate"] == 0.0
    assert report["cues_needing_fix"] == 0
    assert report["cues_compared"] == 4


def test_relabelled_ids_still_score_100_percent_through_the_mapping():
    hypothesis = relabel(make(), {"SPEAKER_00": "SPEAKER_02", "SPEAKER_01": "SPEAKER_00", "SPEAKER_02": "SPEAKER_01"})
    report = evaluate(hypothesis, make())
    assert report["word_accuracy"] == 1.0
    assert report["cue_fix_rate"] == 0.0
    assert report["speaker_mapping"] == {
        "SPEAKER_02": "SPEAKER_00",
        "SPEAKER_00": "SPEAKER_01",
        "SPEAKER_01": "SPEAKER_02",
    }


def test_reference_names_are_used_and_unnamed_ids_still_count():
    reference = make(names={"SPEAKER_00": "Maria", "SPEAKER_01": "Jordan"})  # SPEAKER_02 stays unnamed
    hypothesis = relabel(make(), {"SPEAKER_00": "SPEAKER_01", "SPEAKER_01": "SPEAKER_02", "SPEAKER_02": "SPEAKER_00"})
    report = evaluate(hypothesis, reference)
    assert report["word_accuracy"] == 1.0
    assert report["speaker_mapping"]["SPEAKER_01"] == "Maria"
    assert report["speaker_mapping"]["SPEAKER_00"] == "SPEAKER_02"


def test_one_wrong_cue_is_counted():
    hypothesis = make()
    hypothesis["cues"][1]["speaker"] = "SPEAKER_00"  # the cue only; its words still say SPEAKER_01
    report = evaluate(hypothesis, make())
    assert report["cues_needing_fix"] == 1
    assert report["cue_fix_rate"] == pytest.approx(0.25)
    assert [c["index"] for c in report["cues_to_fix"]] == [1]
    assert report["cues_to_fix"][0]["speaker"] == "SPEAKER_00"
    assert report["cues_to_fix"][0]["expected"] == "SPEAKER_01"
    assert report["word_accuracy"] == 1.0


def test_wrong_words_lower_the_word_share():
    hypothesis = make()
    for word in hypothesis["segments"][1]["words"]:  # all four words of one cue
        word["speaker"] = "SPEAKER_00"
    report = evaluate(hypothesis, make())
    assert report["words_correct"] == 12
    assert report["word_accuracy"] == pytest.approx(12 / 16)


def test_words_with_shifted_timing_still_match():
    hypothesis = make()
    for segment in hypothesis["segments"]:
        for word in segment["words"]:
            word["start"] += 0.13
            word["end"] += 0.13
    for cue in hypothesis["cues"]:
        cue["start"] += 0.13
        cue["end"] += 0.13
    report = evaluate(hypothesis, make())
    assert report["words_matched"] == 16
    assert report["word_accuracy"] == 1.0
    assert report["cue_fix_rate"] == 0.0


def test_shifted_words_do_not_pair_with_the_wrong_neighbour():
    """Each word overlaps its neighbour a little once shifted; the same-text, longer overlap wins."""
    hypothesis = make()
    for word in hypothesis["segments"][0]["words"]:
        word["start"] += 0.2
        word["end"] += 0.2
    hypothesis["segments"][0]["words"][0]["speaker"] = "SPEAKER_01"
    report = evaluate(hypothesis, make())
    assert report["words_matched"] == 16
    assert report["words_correct"] == 15


def test_missing_words_count_against_the_share():
    hypothesis = make()
    del hypothesis["segments"][3]["words"][1:]  # the hypothesis lost two of three words
    report = evaluate(hypothesis, make())
    assert report["words_matched"] == 14
    assert report["word_accuracy"] == pytest.approx(14 / 16)


def test_a_hypothesis_speaker_without_a_partner_is_wrong():
    """Two hypothesis speakers cannot both map to one reference speaker."""
    hypothesis = relabel(make(), {"SPEAKER_00": "SPEAKER_00", "SPEAKER_01": "SPEAKER_01", "SPEAKER_02": "SPEAKER_01"})
    report = evaluate(hypothesis, make())
    # SPEAKER_01 here is two reference speakers; only the larger (4 words vs 3) keeps its partner.
    assert report["words_correct"] == 13
    assert report["cues_needing_fix"] == 1
    assert report["unmapped_speakers"] == []


def test_an_extra_hypothesis_speaker_is_reported_unmapped():
    reference = relabel(make(), {"SPEAKER_00": "SPEAKER_00", "SPEAKER_01": "SPEAKER_01", "SPEAKER_02": "SPEAKER_01"})
    report = evaluate(make(), reference)
    assert report["unmapped_speakers"] == ["SPEAKER_02"]
    assert report["words_correct"] == 13
    assert report["cues_needing_fix"] == 1


def test_assign_finds_the_optimal_pairing_where_greedy_does_not():
    # Greedy would take the 10 at (0, 0) and leave 1; the best pairing is (0, 1) + (1, 0) = 18.
    assert assign([[10, 9], [9, 1]]) == [1, 0]
    assert assign([[5, 0, 0]]) == [0]
    assert assign([[3], [7]]) == [-1, 0]
    assert assign([]) == []


def test_map_speakers_skips_pairs_that_never_overlap():
    assert map_speakers({("a", "x"): 4.0}) == {"a": "x"}
    assert map_speakers({}) == {}


def test_cues_only_transcripts_map_by_cue_time():
    hypothesis = relabel(make(), {"SPEAKER_00": "SPEAKER_01", "SPEAKER_01": "SPEAKER_00", "SPEAKER_02": "SPEAKER_02"})
    for data in (hypothesis, (reference := make())):
        data["segments"] = []
    report = evaluate(hypothesis, reference)
    assert report["words_reference"] == 0
    assert report["cue_fix_rate"] == 0.0
    assert report["speaker_mapping"]["SPEAKER_01"] == "SPEAKER_00"


def test_reference_from_a_saved_edits_file(tmp_path):
    hypothesis = make()
    edits = {
        "version": 1,
        "audio_sha256": None,
        "ops": [
            {"op": "rename", "speaker": "SPEAKER_00", "name": "Maria"},
            {"op": "reassign", "speaker": "SPEAKER_01", "cues": [{"index": 2, "start": 10.0, "end": 11.9}]},
            {"op": "reassign", "speaker": "SPEAKER_01", "cues": [{"index": 9, "start": 99.0, "end": 100.0}]},
        ],
    }
    path = tmp_path / "edits.json"
    path.write_text(json.dumps(edits))
    reference, skipped = eval_speakers.reference_from_edits(hypothesis, path)
    assert skipped == 1  # the cue at 99 s does not exist
    assert reference["cues"][2]["speaker"] == "SPEAKER_01"
    report = evaluate(hypothesis, reference)
    assert report["cues_needing_fix"] == 1
    assert report["cues_to_fix"][0]["index"] == 2
    assert report["words_correct"] == 12  # the four reassigned words are wrong
    assert report["speaker_mapping"]["SPEAKER_00"] == "Maria"


def test_the_script_prints_a_text_and_a_json_report(tmp_path):
    hyp, ref = tmp_path / "hyp.json", tmp_path / "ref.json"
    wrong = make()
    wrong["cues"][0]["speaker"] = "SPEAKER_02"
    hyp.write_text(json.dumps(wrong))
    ref.write_text(json.dumps(make()))
    script = REPO / "scripts" / "eval_speakers.py"

    text = subprocess.run(
        [sys.executable, str(script), str(hyp), "--reference", str(ref)], capture_output=True, text=True
    )
    assert text.returncode == 0, text.stderr
    assert "1 of 4 need a speaker fix" in text.stdout
    assert "100.0%" in text.stdout

    out = subprocess.run(
        [sys.executable, str(script), str(hyp), "--reference", str(ref), "--json"], capture_output=True, text=True
    )
    assert out.returncode == 0, out.stderr
    report = json.loads(out.stdout)
    assert report["cues_needing_fix"] == 1
    assert report["word_accuracy"] == 1.0
