import json

import numpy as np
import pytest

from better_captions import cli
from better_captions import cues as cues_mod
from better_captions.engine import align, diarize, multitrack, transcribe
from better_captions.engine.reassign_segments import TurnScore, reassign_segments, turn_confidence
from better_captions.schema import Segment, SpeakerTurn, Transcript, Word

A = np.array([1.0, 0.0, 0.0])
B = np.array([0.0, 1.0, 0.0])
AB = np.array([1.0, 1.0, 0.0]) / np.sqrt(2)  # sounds like either speaker


def test_turn_confidence_scales_with_the_lead_over_the_next_speaker():
    assert turn_confidence(TurnScore("a", 0.9, 0.1), "a") == 1.0
    assert turn_confidence(TurnScore("a", 0.6, 0.55), "a") == pytest.approx(0.25)
    assert turn_confidence(TurnScore("a", 0.5, 0.5), "a") == 0.0


def test_turn_matching_another_speaker_better_has_no_confidence():
    assert turn_confidence(TurnScore("b", 0.6, 0.55), "a") == 0.0


def test_turn_confidence_unknown_or_single_speaker():
    assert turn_confidence(None, "a") is None
    assert turn_confidence(TurnScore("a", 0.8, None), "a") == 1.0


def test_ambiguous_turn_scores_low_and_clear_turns_high():
    # 'a' and 'b' have eight clear turns each; the last turn sounds like both
    voices = {(0.0, 40.0): A, (40.0, 80.0): B, (90.0, 95.0): AB}
    raw = [(s, s + 5.0, "a" if s < 40 else "b") for s in range(0, 80, 5)] + [(90.0, 95.0, "b")]

    def embed(start, end):
        mid = (start + end) / 2
        return next((v for (s, e), v in voices.items() if s <= mid < e), None)

    result = reassign_segments(raw, embed)
    conf = [turn_confidence(s, lb) for s, (_, _, lb) in zip(result.scores, result.raw, strict=True)]
    assert all(c > 0.9 for c in conf[:-1])
    assert conf[-1] < 0.6


def test_words_take_the_confidence_of_their_turn():
    turns = [SpeakerTurn("S0", 0.0, 2.0, confidence=0.9), SpeakerTurn("S1", 2.0, 4.0, confidence=0.2)]
    words = [Word("hi", 0.5, 1.0), Word("there", 2.5, 3.0), Word("late", 9.0, 9.5)]
    out = align.assign_speakers(words, turns)
    assert [(w.speaker, w.speaker_confidence) for w in out] == [("S0", 0.9), ("S1", 0.2), ("S1", 0.2)]


def test_turns_without_confidence_leave_words_unknown():
    out = align.assign_speakers([Word("hi", 0.0, 0.5)], [SpeakerTurn("S0", 0.0, 1.0)])
    assert out[0].speaker_confidence is None


def test_person_track_words_are_certain():
    assert [w.speaker_confidence for w in multitrack.label_person([Word("hi", 0.0, 0.5)], "SPEAKER_00")] == [1.0]


def _cue_for(words):
    t = Transcript("en", 10.0, segments=[Segment("S0", words[0].start, words[-1].end, "x", words)])
    return cues_mod.build_cues(t)


def test_cue_confidence_is_duration_weighted():
    words = [Word("a", 0.0, 3.0, speaker="S0", speaker_confidence=1.0), Word("b", 3.0, 4.0, speaker="S0")]
    words.append(Word("c", 4.0, 5.0, speaker="S0", speaker_confidence=0.0))
    (cue,) = _cue_for(words)
    assert cue.speaker_confidence == pytest.approx(0.75)


def test_cue_confidence_unknown_when_no_word_has_one():
    (cue,) = _cue_for([Word("a", 0.0, 1.0, speaker="S0")])
    assert cue.speaker_confidence is None


def test_transcript_json_carries_cue_confidence_and_loads_old_files(tmp_path):
    words = [Word("hi", 0.0, 1.0, speaker="S0", speaker_confidence=0.4)]
    t = Transcript("en", 1.0, segments=[Segment("S0", 0.0, 1.0, "hi", words)])
    t.cues = cues_mod.build_cues(t)
    path = tmp_path / "transcript.json"
    t.save(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["cues"][0]["speaker_confidence"] == 0.4
    assert data["schema_version"] == 1
    for cue in data["cues"]:
        del cue["speaker_confidence"]
    for seg in data["segments"]:
        for w in seg["words"]:
            del w["speaker_confidence"]
    assert Transcript.from_dict(data).cues[0].speaker_confidence is None


def test_pipeline_writes_cue_confidence(tmp_path, monkeypatch, capsys):
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"RIFF")
    words = [Word(" Hello.", 0.0, 1.0, 0.9), Word(" Hi.", 2.0, 3.0, 0.9)]
    monkeypatch.setattr(transcribe, "transcribe", lambda path, **kw: ("en", 3.0, words))
    turns = [SpeakerTurn("SPEAKER_00", 0.0, 1.5, 0.95), SpeakerTurn("SPEAKER_01", 1.9, 3.0, 0.1)]
    monkeypatch.setattr(diarize, "diarize_detailed", lambda path, **kw: diarize.DiarizeResult(turns))
    out = tmp_path / "out"
    assert cli.main(["run", str(audio), "--out", str(out), "--naming", "none", "--no-cache", "--no-voice-library"]) == 0
    capsys.readouterr()
    data = json.loads((out / "transcript.json").read_text(encoding="utf-8"))
    assert [c["speaker_confidence"] for c in data["cues"]] == [0.95, 0.1]


def _embed_by_voice(voices):
    def embed(start, end):
        mid = (start + end) / 2
        return next((v for (s, e), v in voices.items() if s <= mid < e), None)

    return embed


def test_ambiguous_turn_on_a_three_turn_speaker_is_not_propped_up_by_itself():
    # 'a' has eight clear turns; 'b' has two clear turns and one that sounds like either speaker. Scored
    # against a centroid that includes it, the ambiguous turn would get ~0.78.
    voices = {(0.0, 40.0): A, (40.0, 50.0): B, (50.0, 55.0): AB}
    raw = [(float(s), s + 5.0, "a") for s in range(0, 40, 5)] + [(40.0, 45.0, "b"), (45.0, 50.0, "b")]
    raw.append((50.0, 55.0, "b"))

    result = reassign_segments(raw, _embed_by_voice(voices))
    assert [lb for _, _, lb in result.raw] == [lb for _, _, lb in raw]
    conf = [turn_confidence(s, lb) for s, (_, _, lb) in zip(result.scores, result.raw, strict=True)]
    assert conf[-1] == pytest.approx(0.0, abs=0.01)
    assert all(c == 1.0 for c in conf[:-1])


def test_returned_centroids_still_include_every_turn():
    voices = {(0.0, 40.0): A, (40.0, 50.0): B, (50.0, 55.0): AB}
    raw = [(float(s), s + 5.0, "a") for s in range(0, 40, 5)] + [(40.0, 45.0, "b"), (45.0, 50.0, "b")]
    raw.append((50.0, 55.0, "b"))

    result = reassign_segments(raw, _embed_by_voice(voices))
    expected_b = (2 * B + AB) / np.linalg.norm(2 * B + AB)
    assert result.centroids["b"] == pytest.approx(expected_b)
    assert result.centroids["a"] == pytest.approx(A)


def test_single_turn_speaker_keeps_its_full_centroid():
    voices = {(0.0, 40.0): A, (40.0, 45.0): B}
    raw = [(float(s), s + 5.0, "a") for s in range(0, 40, 5)] + [(40.0, 45.0, "b")]

    result = reassign_segments(raw, _embed_by_voice(voices))
    assert turn_confidence(result.scores[-1], "b") == 1.0
