"""The plugin against every modelled Resolve release/edition (fake_resolve.PROFILES).

Run alone with ``pytest -m resolve_versions``. Profiles whose free edition gates Python scripting
(21.1+) must stop before touching the project; the rest must run the whole flow identically.
"""

import itertools
import os
import sys

import pytest

from better_captions_plugin import app, compat, resolve_io
from better_captions_plugin.timing import TimelineClock
from tests.resolve.fake_engine import TRANSCRIPT, with_words
from tests.resolve.fake_resolve import PROFILES, FakeTimelineItem, FakeTrack, make_world
from tests.resolve.fake_ui import FakeBackend, accept_all

pytestmark = pytest.mark.resolve_versions

FAKE_ENGINE = os.path.join(os.path.dirname(__file__), "fake_engine.py")
RUNNABLE = [name for name, profile in PROFILES.items() if not profile.python_gated]
GATED = [name for name, profile in PROFILES.items() if profile.python_gated]


@pytest.fixture
def config():
    return {"engine": [sys.executable, "-I", FAKE_ENGINE], "model": "fake-model", "naming_provider": "heuristic"}


def _run(resolve, backend, config):
    return app.main(resolve=resolve, backend=backend, config=config, sleep=lambda s: None)


def _cue_frames(timeline, speaker_id, origin):
    return [
        (origin + int(round(c["start"] * timeline.fps)), "\n".join(c["lines"]))
        for c in TRANSCRIPT["cues"]
        if c["speaker"] == speaker_id
    ]


def _snapshot(timeline):
    """Every existing track's name and clips, to check nothing pre-existing changed."""
    return {
        kind: [(t.name, [(i.GetName(), i.GetStart(), i.GetDuration()) for i in t.items]) for t in tracks]
        for kind, tracks in timeline.tracks.items()
    }


def _with_existing_subtitle_track(resolve):
    tl = resolve.project.timeline
    existing = FakeTimelineItem("Existing line", tl.start_frame + 48, 24)
    tl.tracks["subtitle"].append(FakeTrack("Subtitle 1", [existing]))
    return resolve


# -- detection


@pytest.mark.parametrize("name", list(PROFILES))
def test_detect_matches_profile(name):
    profile = PROFILES[name]
    resolve = make_world(profile=name)
    info = compat.detect(resolve)
    assert info.version == profile.version_tuple
    assert resolve.GetVersion() == [*profile.version_tuple, ""]
    assert info.product == profile.product
    assert info.studio is profile.studio
    assert info.python_gated is profile.python_gated


def test_profiles_cover_the_editions_that_matter():
    assert {"19.0-free", "20.3-free", "20.3-studio", "21.0.4-free", "21.0.4-studio", "21.1.2-studio"} <= set(RUNNABLE)
    assert "21.1.2-free" in GATED
    assert not PROFILES["19.0-free"].has_mark_in_out_api


def test_make_world_defaults_are_unchanged_without_a_profile():
    resolve = make_world()
    assert (resolve.product, resolve.version, resolve.python_gated) == ("DaVinci Resolve", "20.0.0.0023", False)
    assert resolve.GetVersion() == [20, 0, 0, 23, ""]
    assert resolve.project.timeline.has_mark_in_out_api
    # explicit arguments still win over the profile
    assert make_world(profile="19.0-free", has_mark_in_out_api=True).project.timeline.has_mark_in_out_api
    assert make_world(profile="20.3-free", product="DaVinci Resolve Studio").product == "DaVinci Resolve Studio"


# -- resolve_io calls that differ between versions


@pytest.mark.parametrize("name", RUNNABLE)
def test_render_range_per_profile(name):
    tl = make_world(profile=name).project.timeline
    clock = resolve_io.timeline_clock(tl)
    assert resolve_io.render_range(tl, clock) == (86400, None)
    tl.mark_in_out = {"video": {"in": 86500, "out": 86700}, "audio": {"in": 86500, "out": 86700}}
    expected = (86500, 86700) if PROFILES[name].has_mark_in_out_api else (86400, None)
    assert resolve_io.render_range(tl, clock) == expected


@pytest.mark.parametrize("name", RUNNABLE)
@pytest.mark.parametrize(
    "frame_rate, start_timecode, drop_frame, expected",
    [
        ("24", "01:00:00:00", False, TimelineClock(fps=24.0, drop_frame=False, start_frame=86400)),
        ("29.97", "01:00:00;00", True, TimelineClock(fps=29.97, drop_frame=True, start_frame=107892)),
    ],
)
def test_timeline_clock_per_profile(name, frame_rate, start_timecode, drop_frame, expected):
    resolve = make_world(frame_rate=frame_rate, start_timecode=start_timecode, drop_frame=drop_frame, profile=name)
    project = resolve.project
    assert resolve_io.timeline_clock(project.timeline, project) == expected


@pytest.mark.parametrize("name", RUNNABLE)
@pytest.mark.parametrize("marks", [None, (86500, 86700)])
def test_render_audio_per_profile(name, marks, tmp_path):
    project = make_world(profile=name).project
    start, end = marks or (86400, None)
    wav = resolve_io.render_audio(project, project.timeline, str(tmp_path), start, end, sleep=lambda s: None)
    assert os.path.isfile(wav) and wav.endswith(".wav")
    settings = project.last_job["settings"]
    assert settings["ExportVideo"] is False and settings["ExportAudio"] is True
    # 21.1+ asks for WAV with AudioFormat; older builds reject that key and rely on the Audio Only preset.
    assert ("AudioFormat" in settings) is PROFILES[name].audio_render_api
    if marks:
        assert settings["SelectAllFrames"] is False and (settings["MarkIn"], settings["MarkOut"]) == marks
    else:
        tl = project.timeline
        assert (settings["MarkIn"], settings["MarkOut"]) == (tl.GetStartFrame(), tl.GetEndFrame() - 1)
    assert project.last_job["format"] == ("wav", "")  # the Audio Only preset's format
    assert project.render_format == ("mov", "H264") and project.render_mode == 0  # the user's, restored
    assert project.jobs == {} and project.deleted_jobs == ["job-1"]


# -- the whole flow


@pytest.mark.parametrize("name", RUNNABLE)
def test_full_flow_subtitle_tracks(name, config):
    resolve = _with_existing_subtitle_track(make_world(profile=name))
    tl = resolve.project.timeline
    before = _snapshot(tl)
    backend = FakeBackend(accept_all(names={"SPEAKER_01": "Jordan"}, subtitles=True, textplus=False))
    outcome = _run(resolve, backend, config)

    assert outcome.error is None and not outcome.cancelled, backend.errors
    assert backend.errors == []
    assert [t.name for t in tl.tracks["subtitle"]] == ["Subtitle 1", "Maria", "Jordan"]
    for track, speaker_id in (("Maria", "SPEAKER_00"), ("Jordan", "SPEAKER_01")):
        items = tl.track("subtitle", track).items
        assert [(i.GetStart(), i.GetName()) for i in items] == _cue_frames(tl, speaker_id, tl.start_frame)
    after = _snapshot(tl)
    assert after["subtitle"][:1] == before["subtitle"] and after["video"] == before["video"]
    assert after["audio"] == before["audio"]
    assert len(outcome.created) == 2 and outcome.subtitle_fallbacks == []


@pytest.mark.parametrize("name", RUNNABLE)
def test_full_flow_textplus_tracks_with_in_out_marks(name, config):
    resolve = make_world(frame_rate="29.97", start_timecode="01:00:00;00", drop_frame=True, profile=name)
    tl = resolve.project.timeline
    mark_in, mark_out = tl.start_frame + 300, tl.start_frame + 900
    tl.mark_in_out = {"video": {"in": mark_in, "out": mark_out}, "audio": {"in": mark_in, "out": mark_out}}
    before = _snapshot(tl)
    backend = FakeBackend(accept_all(subtitles=False, textplus=True, template_index={"SPEAKER_00": 1, "SPEAKER_01": 2}))
    outcome = _run(resolve, backend, config)

    assert outcome.error is None and backend.errors == [], backend.errors
    # 19.0 builds without GetMarkInOut caption the whole timeline instead of the marked range.
    origin = mark_in if PROFILES[name].has_mark_in_out_api else tl.start_frame
    settings = resolve.project.last_job["settings"]
    if PROFILES[name].has_mark_in_out_api:
        assert (settings["MarkIn"], settings["MarkOut"]) == (mark_in, mark_out)
    else:
        assert (settings["MarkIn"], settings["MarkOut"]) == (tl.GetStartFrame(), tl.GetEndFrame() - 1)
    assert [t.name for t in tl.tracks["video"]] == ["Video 1", "Maria", "SPEAKER_01"]
    assert tl.GetTrackCount("subtitle") == 0
    maria = tl.track("video", "Maria").items
    assert [(i.GetStart(), i.text()) for i in maria] == _cue_frames(tl, "SPEAKER_00", origin)
    assert [(i.GetStart(), i.text()) for i in tl.track("video", "SPEAKER_01").items] == _cue_frames(
        tl, "SPEAKER_01", origin
    )
    assert maria[0].media_pool_item.name == "Caption Style A"
    after = _snapshot(tl)
    assert after["video"][:1] == before["video"] and after["audio"] == before["audio"]


@pytest.mark.parametrize("name", RUNNABLE)
def test_full_flow_textplus_with_word_highlighting(name, config):
    config["engine_args"] = ["--with-words"]
    resolve = make_world(profile=name)
    tl = resolve.project.timeline
    before = _snapshot(tl)
    backend = FakeBackend(accept_all(subtitles=False, textplus=True, highlight_words=True, highlight_color="#FF0000"))
    outcome = _run(resolve, backend, config)

    assert outcome.error is None and backend.errors == [], backend.errors
    words = [w for s in with_words(TRANSCRIPT)["segments"] if s["speaker"] == "SPEAKER_00" for w in s["words"]]
    maria = tl.track("video", "Maria").items
    assert len(maria) == len(words) == 9
    assert all(i.highlight() and i.highlight()["text"] == i.text() for i in maria)
    assert maria[0].GetStart() == tl.start_frame + 12 and maria[-1].GetEnd() == tl.start_frame + 156
    assert all(a.GetEnd() <= b.GetStart() for a, b in itertools.pairwise(maria))
    assert "spoken word highlighted in 9 clips" in backend.messages[0][1]
    after = _snapshot(tl)
    assert after["video"][:1] == before["video"] and after["audio"] == before["audio"]


# -- editions where Python gets no API (free 21.1+); contract for app.main


def _assert_nothing_touched(resolve, before, backend):
    project = resolve.project
    assert project.jobs == {} and project.deleted_jobs == [] and project.render_settings == {}
    assert _snapshot(project.timeline) == before
    assert backend.requests == [] and backend.messages == []


@pytest.mark.parametrize("name", GATED)
def test_gated_edition_explains_the_studio_requirement(name, config):
    resolve = make_world(profile=name)
    before = _snapshot(resolve.project.timeline)
    backend = FakeBackend(accept_all(subtitles=True, textplus=True))
    outcome = _run(resolve, backend, config)

    expected = compat.gated_message(compat.detect(resolve))
    assert outcome.error == expected
    assert backend.errors == [(app.TITLE, expected)]
    _assert_nothing_touched(resolve, before, backend)


def test_no_resolve_object_says_python_needs_studio(config):
    backend = FakeBackend(accept_all())
    outcome = _run(None, backend, config)
    assert outcome.error == compat.NO_RESOLVE_MESSAGE
    assert backend.errors == [(app.TITLE, compat.NO_RESOLVE_MESSAGE)]
    assert backend.requests == [] and backend.messages == []
