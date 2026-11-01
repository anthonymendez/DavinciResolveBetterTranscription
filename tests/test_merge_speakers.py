"""Minor speaker clusters are folded into the most likely real speaker (fake embeddings; sherpa faked)."""

import sys
import types
from types import SimpleNamespace as NS

import numpy as np
import pytest

from better_captions.engine import diarize as dz
from better_captions.engine import merge_speakers as ms

# --- merge_minor_speakers on raw turns -----------------------------------------

# Three real speakers with 100 s each, and two stray clusters right after C finishes talking.
# Voices: A, B and C are orthogonal; X sounds like B, Y like A. In time, both are nearest to C.
MAJOR = [
    (0.0, 40.0, "A"), (40.0, 70.0, "B"), (70.0, 100.0, "C"),
    (100.0, 130.0, "A"), (130.0, 160.0, "B"), (160.0, 200.0, "C"),
    (200.0, 230.0, "A"), (230.0, 270.0, "B"), (270.0, 300.0, "C"),
]  # fmt: skip
MINOR = [(302.0, 305.0, "X"), (310.0, 314.0, "Y")]
VOICE = {"A": [1, 0, 0], "B": [0, 1, 0], "C": [0, 0, 1], "X": [0.1, 0.9, 0.2], "Y": [0.8, 0.1, 0.3]}


def fake_embed(turns, calls=None):
    """An embedder that returns the voice of whichever turn covers [start, end)."""

    def embed(start, end):
        if calls is not None:
            calls.append((start, end))
        mid = (start + end) / 2
        label = next(lb for s, e, lb in turns if s <= mid < e)
        return np.array(VOICE[label], dtype=np.float32)

    return embed


def labels(raw):
    return [lb for _, _, lb in raw]


def test_minor_clusters_merge_into_most_similar_major_by_embedding():
    raw = MAJOR + MINOR
    result = ms.merge_minor_speakers(raw, fake_embed(raw))
    assert labels(result.raw) == [*labels(MAJOR), "B", "A"]
    assert [(s, e) for s, e, _ in result.raw] == [(s, e) for s, e, _ in raw]
    assert (result.minor, result.remaining, result.by_embedding, result.by_time) == (2, 3, 2, 0)


def test_each_minor_turn_is_matched_on_its_own():
    # One stray cluster whose turns sound like different people goes to different speakers.
    raw = [*MAJOR, (302.0, 305.0, "X"), (310.0, 314.0, "X")]
    voices_heard = [*MAJOR, (302.0, 305.0, "X"), (310.0, 314.0, "Y")]
    result = ms.merge_minor_speakers(raw, fake_embed(voices_heard))
    assert labels(result.raw)[-2:] == ["B", "A"]


def test_centroids_come_from_the_longest_turns_cropped(monkeypatch):
    monkeypatch.setattr(ms, "CENTROID_TURNS", 2)
    raw = MAJOR + MINOR
    calls = []
    ms.merge_minor_speakers(raw, fake_embed(raw, calls))
    # A's two longest turns: 0-40 (cropped to its middle 10 s) and one of its 30 s turns.
    assert (15.0, 25.0) in calls
    assert all(e - s <= ms.MAX_EMBED_SECONDS for s, e in calls)
    assert len([c for c in calls if c[0] < 300]) == 6  # 2 per major speaker, nothing more
    assert sorted(c for c in calls if c[0] >= 300) == [(302.0, 305.0), (310.0, 314.0)]  # each minor turn once


def test_temporal_fallback_without_embeddings():
    result = ms.merge_minor_speakers(MAJOR + MINOR, None)
    assert labels(result.raw) == [*labels(MAJOR), "C", "C"]
    assert (result.by_embedding, result.by_time) == (0, 2)


def test_temporal_fallback_when_the_embedder_returns_nothing():
    result = ms.merge_minor_speakers(MAJOR + MINOR, lambda s, e: None)
    assert labels(result.raw) == [*labels(MAJOR), "C", "C"]


def test_temporal_fallback_picks_the_nearest_turn_and_overlap_wins():
    raw = [(0.0, 100.0, "A"), (200.0, 300.0, "B"), (110.0, 112.0, "X"), (190.0, 195.0, "Y"), (250.0, 251.0, "Z")]
    assert labels(ms.merge_minor_speakers(raw).raw) == ["A", "B", "A", "B", "B"]


def test_unembeddable_turn_uses_its_clusters_voice():
    raw = [*MAJOR, (302.0, 305.0, "X"), (306.0, 306.2, "X")]

    def embed(start, end):
        return None if end - start < 0.5 else fake_embed(MAJOR + MINOR)(start, end)

    result = ms.merge_minor_speakers(raw, embed)
    assert labels(result.raw)[-2:] == ["B", "B"]  # not C, the nearest in time
    assert result.by_embedding == 2


def test_no_op_when_every_cluster_is_major():
    result = ms.merge_minor_speakers(MAJOR, fake_embed(MAJOR))
    assert result.raw == MAJOR
    assert (result.minor, result.remaining) == (0, 3)


def test_no_op_when_every_cluster_is_minor():
    raw = [(float(i), i + 0.5, i) for i in range(100)]  # 100 clusters, 1% of the speech each
    result = ms.merge_minor_speakers(raw)
    assert result.raw == raw and result.minor == 0


def test_short_clips_keep_their_speakers():
    # 2 s of a 7 s clip is under MINOR_SECONDS but far too large a share to be a stray cluster.
    raw = [(0.0, 2.0, 0), (2.0, 4.0, 1), (4.0, 6.0, 2), (6.0, 7.0, 0)]
    assert ms.merge_minor_speakers(raw).minor == 0


def test_thresholds_are_tunable():
    raw = MAJOR + MINOR
    assert ms.merge_minor_speakers(raw, min_seconds=3.5, min_share=0.0).minor == 1  # only the 3 s cluster
    assert ms.merge_minor_speakers(raw, min_seconds=0.0, min_share=0.0).minor == 0


def test_threshold_scales_with_long_recordings():
    assert ms.minor_threshold(7.0) == pytest.approx(0.35)  # capped at 5% of a short clip
    assert ms.minor_threshold(600.0) == pytest.approx(15.0)
    assert ms.minor_threshold(9000.0) == pytest.approx(180.0)  # 2% of 2.5 h


def test_keep_at_least_stops_the_merge_smallest_first():
    raw = MAJOR + MINOR
    result = ms.merge_minor_speakers(raw, fake_embed(raw), keep_at_least=4)
    assert labels(result.raw) == [*labels(MAJOR), "B", "Y"]  # X (3 s) folded, Y (4 s) kept
    assert (result.minor, result.remaining) == (1, 4)
    assert ms.merge_minor_speakers(raw, fake_embed(raw), keep_at_least=5).minor == 0


# --- through diarize() ---------------------------------------------------------

SR = 16000
LEVEL = {0: 0.1, 1: 0.5, 2: 0.9}  # what each voice "sounds like" in the fake audio
# sherpa labels: 0, 1, 2 are A, B, C; 3 sounds like B, 4 like A.
SHERPA_TURNS = [(s, e, "ABC".index(lb)) for s, e, lb in MAJOR] + [(302.0, 305.0, 3), (310.0, 314.0, 4)]
TURN_LEVEL = {0: 0.1, 1: 0.5, 2: 0.9, 3: 0.5, 4: 0.1}


class _Config:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

    def validate(self):
        return True


def make_fake_sherpa(results, with_extractor=True):
    runs, extracted = [], []

    class OfflineSpeakerDiarization:
        sample_rate = SR

        def __init__(self, config):
            self.config = config

        def process(self, samples, callback=None):
            num_clusters = self.config.clustering.num_clusters
            runs.append(num_clusters)
            callback(1, 1)
            segments = [NS(start=s, end=e, speaker=k) for s, e, k in results[num_clusters]]
            return NS(sort_by_start_time=lambda: sorted(segments, key=lambda s: s.start))

    class Stream:
        def accept_waveform(self, sample_rate, waveform):
            assert sample_rate == SR
            self.waveform = waveform

        def input_finished(self):
            pass

    class SpeakerEmbeddingExtractor:
        def __init__(self, config):
            self.config = config

        def create_stream(self):
            return Stream()

        def is_ready(self, stream):
            return True

        def compute(self, stream):
            extracted.append(len(stream.waveform) / SR)
            vec = [0.0] * 10
            vec[int(round(float(np.mean(stream.waveform)) * 10))] = 1.0  # one-hot of the voice's level
            return vec

    mod = types.SimpleNamespace(
        OfflineSpeakerDiarizationConfig=_Config,
        OfflineSpeakerSegmentationModelConfig=_Config,
        OfflineSpeakerSegmentationPyannoteModelConfig=_Config,
        SpeakerEmbeddingExtractorConfig=_Config,
        FastClusteringConfig=_Config,
        OfflineSpeakerDiarization=OfflineSpeakerDiarization,
    )
    if with_extractor:
        mod.SpeakerEmbeddingExtractor = SpeakerEmbeddingExtractor
    return mod, runs, extracted


@pytest.fixture
def sherpa(monkeypatch, tmp_path):
    audio = np.zeros(320 * SR, np.float32)
    for s, e, k in SHERPA_TURNS:
        audio[int(s * SR) : int(e * SR)] = TURN_LEVEL[k]

    def install(results=None, with_extractor=True):
        mod, runs, extracted = make_fake_sherpa(results or {-1: SHERPA_TURNS}, with_extractor)
        monkeypatch.setitem(sys.modules, "sherpa_onnx", mod)
        return NS(runs=runs, extracted=extracted)

    monkeypatch.setattr(dz, "sherpa_models", lambda: (tmp_path / "seg.onnx", tmp_path / "emb.onnx"))
    monkeypatch.setattr(dz, "load_audio", lambda path, sr=dz.SAMPLE_RATE: audio)
    return install


def speakers_of_last_two(turns):
    return [t.speaker for t in turns[-2:]]


def test_diarize_merges_minor_speakers_by_voice_and_logs_it(sherpa):
    fake = sherpa()
    lines = []
    turns = dz.diarize("in.wav", log=lines.append)
    assert sorted({t.speaker for t in turns}) == ["SPEAKER_00", "SPEAKER_01", "SPEAKER_02"]
    assert speakers_of_last_two(turns) == ["SPEAKER_01", "SPEAKER_00"]  # B and A, not C
    assert "Detecting speakers: merged 2 minor speakers into 3" in lines
    assert lines[-2] == "Detecting speakers: found 3 speakers"
    assert fake.extracted and max(fake.extracted) <= ms.MAX_EMBED_SECONDS


def test_diarize_falls_back_to_time_without_an_extractor(sherpa):
    sherpa(with_extractor=False)
    lines = []
    turns = dz.diarize("in.wav", log=lines.append)
    assert speakers_of_last_two(turns) == ["SPEAKER_02", "SPEAKER_02"]  # C, nearest in time
    assert any(line.startswith("Detecting speakers: voice embeddings unavailable") for line in lines)
    assert "Detecting speakers: merged 2 minor speakers into 3" in lines


def test_forced_speaker_count_that_matches_is_respected(sherpa):
    fake = sherpa({5: SHERPA_TURNS})
    lines = []
    turns = dz.diarize("in.wav", num_speakers=5, log=lines.append, reassign=False)
    assert fake.runs == [5]
    assert len({t.speaker for t in turns}) == 5
    assert not any("merged" in line for line in lines)
    assert fake.extracted == []  # nothing to merge: the extractor never runs


def test_forced_count_below_what_was_found_merges_down_to_it(sherpa):
    # pyannote can return more speakers than asked for; the merge stops at the forced count.
    sherpa({4: SHERPA_TURNS})
    lines = []
    turns = dz.diarize("in.wav", num_speakers=4, log=lines.append)
    assert len({t.speaker for t in turns}) == 4
    assert "Detecting speakers: merged 1 minor speaker into 4" in lines


def test_min_speakers_is_a_floor(sherpa):
    sherpa()
    turns = dz.diarize("in.wav", min_speakers=5, max_speakers=6, log=lambda line: None)
    assert len({t.speaker for t in turns}) == 5


def test_merge_can_be_turned_off(sherpa):
    sherpa()
    lines = []
    turns = dz.diarize("in.wav", merge_minor=False, log=lines.append)
    assert len({t.speaker for t in turns}) == 5
    assert not any("merged" in line for line in lines)


def test_diarize_thresholds_are_passed_through(sherpa):
    sherpa()
    turns = dz.diarize("in.wav", minor_seconds=3.5, minor_share=0.0, log=lambda line: None)
    assert len({t.speaker for t in turns}) == 4
