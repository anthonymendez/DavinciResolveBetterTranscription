import sys
import types

import numpy as np
import pytest

from better_captions.engine import diarize as diarize_mod
from better_captions.engine.reassign_segments import reassign_segments

A = np.array([1.0, 0.0, 0.0])
B = np.array([0.0, 1.0, 0.0])


def fake_embed(voices):
    """embed(start, end) -> the vector of the voice whose span contains the middle of [start, end)."""

    def embed(start, end):
        mid = (start + end) / 2
        for (s, e), vec in voices.items():
            if s <= mid < e:
                return vec
        return None

    return embed


def scenario(extra_voices, extra_turns):
    """Speaker 'a' talks 0-40 s, speaker 'b' 40-80 s; extra turns/voices are added on top."""
    voices = {(0.0, 40.0): A, (40.0, 80.0): B, **extra_voices}
    raw = [(0.0, 20.0, "a"), (20.0, 40.0, "a"), (40.0, 60.0, "b"), (60.0, 80.0, "b"), *extra_turns]
    return raw, fake_embed(voices)


def test_turn_in_the_wrong_cluster_moves():
    # 90-95 s sounds like A but was clustered into B.
    raw, embed = scenario({(90.0, 95.0): A}, [(90.0, 95.0, "b")])
    result = reassign_segments(raw, embed)
    assert result.raw[-1] == (90.0, 95.0, "a")
    assert result.raw[:-1] == raw[:-1]
    assert result.moved == 1


def long_scenario(extra_voices, extra_turns):
    """Eight 5 s turns per speaker, so a shorter extra turn never feeds the centroids it is judged against."""
    voices = {(0.0, 40.0): A, (40.0, 80.0): B, **extra_voices}
    raw = [(5.0 * i, 5.0 * i + 5.0, "a" if i < 8 else "b") for i in range(16)] + extra_turns
    return raw, fake_embed(voices)


# Cosine similarity to A is 0.68, to B 0.74: B is closer, but by less than the 0.1 margin.
AMBIGUOUS = np.array([0.55, 0.6, 0.0])


def test_ambiguous_turn_stays():
    raw, embed = long_scenario({(90.0, 94.0): AMBIGUOUS}, [(90.0, 94.0, "a")])
    result = reassign_segments(raw, embed)
    assert result.raw[-1][2] == "a"
    assert result.moved == 0
    # ...but it is still scored against both voices.
    score = result.scores[-1]
    assert score.label == "b"
    assert 0 < score.best - score.second < 0.1


def test_margin_is_configurable():
    raw, embed = long_scenario({(90.0, 94.0): AMBIGUOUS}, [(90.0, 94.0, "a")])
    assert reassign_segments(raw, embed, margin=0.01).raw[-1][2] == "b"


def test_short_turns_stay():
    raw, embed = scenario({(90.0, 90.5): A}, [(90.0, 90.5, "b")])
    result = reassign_segments(raw, embed)
    assert result.raw[-1] == (90.0, 90.5, "b")
    assert result.moved == 0
    assert result.scores[-1] is None
    # The same turn long enough is moved, so it is the length that kept it.
    raw, embed = scenario({(90.0, 92.0): A}, [(90.0, 92.0, "b")])
    assert reassign_segments(raw, embed).raw[-1][2] == "a"


def test_turn_without_an_embedding_stays():
    raw, embed = scenario({}, [(90.0, 95.0, "b")])  # nothing covers 90-95 s -> embed returns None
    result = reassign_segments(raw, embed)
    assert result.raw[-1] == (90.0, 95.0, "b")
    assert result.scores[-1] is None


def test_no_embedder_is_a_noop():
    raw = [(0.0, 20.0, "a"), (20.0, 40.0, "b")]
    result = reassign_segments(raw, None)
    assert result.raw == raw
    assert (result.moved, result.centroids, result.scores) == (0, {}, [None, None])


def test_exposes_centroids_and_similarities():
    raw, embed = scenario({(90.0, 95.0): A}, [(90.0, 95.0, "b")])
    result = reassign_segments(raw, embed)
    assert set(result.centroids) == {"a", "b"}
    np.testing.assert_allclose(result.centroids["a"], A, atol=1e-9)
    np.testing.assert_allclose(result.centroids["b"], B, atol=1e-9)
    assert len(result.scores) == len(result.raw)
    first = result.scores[0]
    assert first.label == "a"
    assert first.best == pytest.approx(1.0)
    assert first.second == pytest.approx(0.0)
    assert result.scores[-1].label == "a"  # scored after the move


def test_centroids_are_rebuilt_after_a_move():
    # Without iterating, the lone wrong-cluster turn would still pull B's centroid. Two B turns that are
    # really A drag B's centroid towards A; once they are moved the centroid must be pure B again.
    voices = {(90.0, 95.0): A, (100.0, 105.0): A}
    raw, embed = scenario(voices, [(90.0, 95.0, "b"), (100.0, 105.0, "b")])
    result = reassign_segments(raw, embed)
    assert [r[2] for r in result.raw[-2:]] == ["a", "a"]
    np.testing.assert_allclose(result.centroids["b"], B, atol=1e-9)


def test_never_empties_a_speaker():
    voices = {(0.0, 10.0): A, (10.0, 20.0): B, (20.0, 30.0): A}
    raw = [(0.0, 10.0, "a"), (10.0, 20.0, "b"), (20.0, 30.0, "a")]
    # 'b' has a single turn, which sounds like B; make it sound like A instead.
    voices[(10.0, 20.0)] = A
    result = reassign_segments(raw, fake_embed(voices))
    assert {r[2] for r in result.raw} == {"a", "b"}


def test_single_speaker_is_untouched():
    raw = [(0.0, 20.0, "a"), (20.0, 40.0, "a")]
    result = reassign_segments(raw, fake_embed({(0.0, 40.0): A}))
    assert result.raw == raw
    assert result.scores[0].second is None


# --- wired into diarize() ----------------------------------------------------


class FakeSegments(list):
    def sort_by_start_time(self):
        return self


def sherpa_with_embedding(monkeypatch, tmp_path, raw_turns, voices):
    """Run diarize with sherpa stubbed to return raw_turns and an embedder backed by `voices`."""
    monkeypatch.setitem(sys.modules, "sherpa_onnx", types.ModuleType("sherpa_onnx"))
    embed = fake_embed(voices)
    monkeypatch.setattr(diarize_mod, "_diarize_sherpa", lambda *a, **k: ([(s, e, lb) for s, e, lb in raw_turns], embed))
    return embed


RAW = [(0.0, 40.0, 0), (40.0, 80.0, 1), (90.0, 95.0, 1)]
VOICES = {(0.0, 40.0): A, (40.0, 80.0): B, (90.0, 95.0): A}


def test_diarize_reassigns_by_default(monkeypatch, tmp_path):
    sherpa_with_embedding(monkeypatch, tmp_path, RAW, VOICES)
    turns = diarize_mod.diarize("x.wav", log=lambda m: None, merge_minor=False)
    assert [t.speaker for t in turns] == ["SPEAKER_00", "SPEAKER_01", "SPEAKER_00"]


def test_diarize_reassign_can_be_disabled(monkeypatch, tmp_path):
    sherpa_with_embedding(monkeypatch, tmp_path, RAW, VOICES)
    turns = diarize_mod.diarize("x.wav", log=lambda m: None, merge_minor=False, reassign=False)
    assert [t.speaker for t in turns] == ["SPEAKER_00", "SPEAKER_01", "SPEAKER_01"]


def test_diarize_detailed_exposes_centroids_and_scores(monkeypatch, tmp_path):
    sherpa_with_embedding(monkeypatch, tmp_path, RAW, VOICES)
    logs = []
    result = diarize_mod.diarize_detailed("x.wav", log=logs.append, merge_minor=False)
    assert [t.speaker for t in result.turns] == ["SPEAKER_00", "SPEAKER_01", "SPEAKER_00"]
    assert set(result.centroids) == {"SPEAKER_00", "SPEAKER_01"}
    assert len(result.scores) == len(result.turns)
    assert [s.label for s in result.scores] == ["SPEAKER_00", "SPEAKER_01", "SPEAKER_00"]
    assert any("moved 1 turn" in m for m in logs)


def test_diarize_detailed_scores_follow_sorted_turns(monkeypatch, tmp_path):
    unsorted = [(90.0, 95.0, 1), (40.0, 80.0, 1), (0.0, 40.0, 0)]
    sherpa_with_embedding(monkeypatch, tmp_path, unsorted, VOICES)
    result = diarize_mod.diarize_detailed("x.wav", log=lambda m: None, merge_minor=False)
    assert [(t.start, t.speaker) for t in result.turns] == [
        (0.0, "SPEAKER_00"),
        (40.0, "SPEAKER_01"),
        (90.0, "SPEAKER_00"),
    ]
    assert [s.label for s in result.scores] == ["SPEAKER_00", "SPEAKER_01", "SPEAKER_00"]


def test_diarize_without_an_embedder_is_a_noop(monkeypatch):
    monkeypatch.setattr(diarize_mod, "_diarize_pyannote", lambda *a, **k: list(RAW))
    result = diarize_mod.diarize_detailed("x.wav", backend="pyannote", log=lambda m: None, merge_minor=False)
    assert [t.speaker for t in result.turns] == ["SPEAKER_00", "SPEAKER_01", "SPEAKER_01"]
    assert result.centroids == {}
    assert result.scores == [None, None, None]
