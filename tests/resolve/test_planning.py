import json

import pytest

from better_captions_plugin import planning, transcript as transcript_mod
from better_captions_plugin.planning import ClipPlacement, place_cues, plan_tracks, unique_name
from better_captions_plugin.timing import TimelineClock
from better_captions_plugin.transcript import Cue

from tests.resolve.fake_engine import TRANSCRIPT


@pytest.fixture
def transcript():
    return transcript_mod.from_dict(json.loads(json.dumps(TRANSCRIPT)))


def test_transcript_loader_matches_engine_shape(transcript, tmp_path):
    assert [s.id for s in transcript.speakers] == ["SPEAKER_00", "SPEAKER_01"]
    assert transcript.speaker("SPEAKER_00").display_name == "Maria"
    assert transcript.speaker("SPEAKER_01").display_name == "SPEAKER_01"
    assert transcript.cues[0].text == "Hey Jordan,\nhow are you?"
    assert transcript.speaker_ids_with_cues() == ["SPEAKER_00", "SPEAKER_01"]
    path = tmp_path / "transcript.json"
    path.write_text(json.dumps(TRANSCRIPT))
    assert transcript_mod.load(path).duration == 12.0


def test_transcript_loader_rejects_newer_schema_and_garbage(tmp_path):
    with pytest.raises(transcript_mod.TranscriptError):
        transcript_mod.from_dict({"language": "en", "duration": 1, "schema_version": 99})
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(transcript_mod.TranscriptError):
        transcript_mod.load(bad)


def test_speakers_without_cues_are_skipped_and_unknown_speaker_ids_kept():
    t = transcript_mod.from_dict({
        "language": "en", "duration": 5,
        "speakers": [{"id": "A"}, {"id": "B"}],
        "cues": [{"speaker": "B", "start": 0, "end": 1, "lines": ["x"]}, {"speaker": "Z", "start": 1, "end": 2, "lines": ["y"]}],
    })
    assert t.speaker_ids_with_cues() == ["B", "Z"]


def test_place_cues_maps_seconds_to_absolute_frames_from_origin():
    clock = TimelineClock(fps=24, start_frame=86400)
    cues = [Cue("S", 0.5, 2.0, ["a"]), Cue("S", 4.0, 6.5, ["b"])]
    clips = place_cues(cues, clock)
    assert clips == [
        ClipPlacement(record_frame=86412, duration=36, text="a", start=0.5, end=2.0),
        ClipPlacement(record_frame=86496, duration=60, text="b", start=4.0, end=6.5),
    ]
    # With an in-point, cues count from there instead of the timeline start.
    assert place_cues(cues, clock, origin_frame=90000)[0].record_frame == 90012


def test_place_cues_never_overlaps_and_is_at_least_one_frame():
    clock = TimelineClock(fps=24, start_frame=0)
    cues = [Cue("S", 1.0, 3.0, ["long"]), Cue("S", 2.0, 2.5, ["inside"]), Cue("S", 2.5, 2.51, ["tiny"]), Cue("S", 2.5, 2.52, ["same start"])]
    clips = place_cues(cues, clock)
    for a, b in zip(clips, clips[1:]):
        assert a.end_frame <= b.record_frame, (a, b)
        assert a.duration >= 1
    assert [c.text for c in clips] == ["long", "inside", "tiny", "same start"]
    assert [c.record_frame for c in clips] == [24, 48, 60, 61]  # starts kept; equal start pushed one frame
    assert clips[0].duration == 24  # trimmed to end where "inside" starts (frame 48)
    assert clips[1].duration == 12 and clips[2].duration == 1 and clips[3].duration == 1


def test_place_cues_sorts_by_start():
    clock = TimelineClock(fps=25, start_frame=0)
    clips = place_cues([Cue("S", 5.0, 6.0, ["second"]), Cue("S", 1.0, 2.0, ["first"])], clock)
    assert [c.text for c in clips] == ["first", "second"]


def test_unique_name_is_case_insensitive_and_counts_up():
    assert unique_name("Maria", []) == "Maria"
    assert unique_name("Maria", ["maria"]) == "Maria 2"
    assert unique_name("Maria", ["Maria", "Maria 2", "MARIA 3"]) == "Maria 4"


def test_plan_tracks_names_tracks_after_speakers_and_avoids_existing(transcript):
    clock = TimelineClock(fps=24, start_frame=86400)
    plans = plan_tracks(
        transcript,
        {"SPEAKER_01": "  Jordan  "},
        planning.MODE_TEXTPLUS,
        clock,
        existing_track_names=["Video 1", "Maria"],
        templates={"SPEAKER_00": "tplA"},
    )
    assert [(p.speaker_id, p.name) for p in plans] == [("SPEAKER_00", "Maria 2"), ("SPEAKER_01", "Jordan")]
    assert plans[0].template == "tplA" and plans[1].template is None
    assert [c.record_frame for c in plans[0].clips] == [86412, 86496]
    assert [c.text for c in plans[1].clips] == ["Good, thanks Maria.", "Always."]
    assert plans[0].first_frame == 86412


def test_plan_tracks_blank_name_falls_back_to_speaker_id_and_duplicates_get_numbered(transcript):
    clock = TimelineClock(fps=24)
    plans = plan_tracks(transcript, {"SPEAKER_00": "Sam", "SPEAKER_01": "sam"}, planning.MODE_SUBTITLES, clock,
                        srt_paths={"SPEAKER_00": "/tmp/a.srt"})
    assert [p.name for p in plans] == ["Sam", "sam 2"]
    assert plans[0].srt_path == "/tmp/a.srt" and plans[1].srt_path is None
    plans = plan_tracks(transcript, {"SPEAKER_01": "   "}, planning.MODE_SUBTITLES, clock)
    assert plans[1].name == "SPEAKER_01"


def test_plan_tracks_rejects_unknown_mode(transcript):
    with pytest.raises(ValueError):
        plan_tracks(transcript, {}, "bogus", TimelineClock(fps=24))
