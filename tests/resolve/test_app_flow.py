"""The whole flow (render -> engine subprocess -> review -> tracks) against the fakes."""

import os
import sys

import pytest

from better_captions_plugin import app
from better_captions_plugin.ui import ReviewResult

from tests.resolve.fake_engine import TRANSCRIPT
from tests.resolve.fake_resolve import FakeMediaPoolItem, make_world
from tests.resolve.fake_ui import FakeBackend, accept_all

FAKE_ENGINE = os.path.join(os.path.dirname(__file__), "fake_engine.py")


@pytest.fixture
def config():
    return {"engine": [sys.executable, "-I", FAKE_ENGINE], "model": "fake-model", "naming_provider": "heuristic"}


def _run(resolve, backend, config, **kwargs):
    kwargs.setdefault("sleep", lambda s: None)
    return app.main(resolve=resolve, backend=backend, config=config, **kwargs)


def _cue_frames(timeline, speaker_id, origin=None):
    origin = timeline.start_frame if origin is None else origin
    return [
        (origin + int(round(c["start"] * timeline.fps)), "\n".join(c["lines"]))
        for c in TRANSCRIPT["cues"]
        if c["speaker"] == speaker_id
    ]


def test_full_flow_subtitle_tracks_with_renamed_speakers(config):
    resolve = make_world(frame_rate="24", start_timecode="01:00:00:00")
    backend = FakeBackend(accept_all(names={"SPEAKER_01": "Jordan"}, subtitles=True, textplus=False))
    outcome = _run(resolve, backend, config)

    assert outcome.error is None and not outcome.cancelled, backend.errors
    tl = resolve.project.timeline
    # Progress dialog saw the render, the engine stages and got closed.
    view = backend.progress_views[0]
    assert view.closed and view.statuses[0] == "Rendering timeline audio…"
    assert "Transcribing…" in view.statuses and "Detecting speakers…" in view.statuses
    assert any("not json: model loaded" in line for line in view.lines)
    assert view.progress[-1] == 100
    # The engine was invoked per the contract with the rendered WAV and the user's settings.
    assert (
        resolve.project.render_settings["ExportAudio"] is True
        and resolve.project.render_settings["ExportVideo"] is False
    )
    engine_line = next(line for line in view.lines if line.startswith("Engine: "))
    assert "--model fake-model --naming heuristic" in engine_line and ".wav --out" in engine_line
    # The review dialog got the engine's name guess, cue counts, samples and the templates from all bins.
    request = backend.requests[0]
    assert [(r.id, r.name, r.cue_count) for r in request.speakers] == [
        ("SPEAKER_00", "Maria", 2),
        ("SPEAKER_01", "SPEAKER_01", 2),
    ]
    assert request.speakers[0].sample == "Hey Jordan,\nhow are you?"
    assert request.templates == [
        "Plain Text+  (Master/Templates)",
        "Caption Style A  (Master/Templates/Lower Thirds)",
        "Caption Style B  (Master/Templates/Lower Thirds)",
    ]
    assert request.timeline_name == "Timeline 1" and request.range_label == "whole timeline (from 01:00:00:00)"
    # One subtitle track per speaker, named after the (renamed) speaker, cues at the right frames.
    assert [t.name for t in tl.tracks["subtitle"]] == ["Maria", "Jordan"]
    assert [(i.GetStart(), i.GetName()) for i in tl.track("subtitle", "Maria").items] == _cue_frames(tl, "SPEAKER_00")
    assert [(i.GetStart(), i.GetName()) for i in tl.track("subtitle", "Jordan").items] == _cue_frames(tl, "SPEAKER_01")
    # Existing tracks untouched; no video tracks added in subtitle mode.
    assert [t.name for t in tl.tracks["video"]] == ["Video 1"] and len(tl.tracks["video"][0].items) == 1
    # Summary shown, temp dir cleaned up.
    assert backend.messages and 'subtitle track 1 "Maria" with 2 clips' in backend.messages[0][1]
    assert outcome.work_dir and not os.path.exists(outcome.work_dir)
    assert backend.closed is False  # injected backends are not closed by main


def test_full_flow_textplus_tracks_on_drop_frame_timeline_with_in_out_range(config):
    resolve = make_world(frame_rate="29.97", start_timecode="01:00:00;00", drop_frame=True)
    tl = resolve.project.timeline
    mark_in, mark_out = tl.start_frame + 300, tl.start_frame + 900
    tl.mark_in_out = {"video": {"in": mark_in, "out": mark_out}, "audio": {"in": mark_in, "out": mark_out}}
    backend = FakeBackend(accept_all(subtitles=False, textplus=True, template_index={"SPEAKER_00": 1, "SPEAKER_01": 2}))
    outcome = _run(resolve, backend, config)

    assert outcome.error is None, backend.errors
    assert (resolve.project.render_settings["MarkIn"], resolve.project.render_settings["MarkOut"]) == (
        mark_in,
        mark_out,
    )
    assert backend.requests[0].range_label == "01:00:10;00 – 01:00:30;00"
    assert [t.name for t in tl.tracks["video"]] == ["Video 1", "Maria", "SPEAKER_01"]
    assert tl.GetTrackCount("subtitle") == 0
    maria = tl.track("video", "Maria").items
    # Cues count from the in-point, at 29.97 fps (0.5 s -> 15 frames).
    assert [(i.GetStart(), i.text()) for i in maria] == _cue_frames(tl, "SPEAKER_00", origin=mark_in)
    assert maria[0].GetDuration() == int(round(2.0 * 29.97)) - int(round(0.5 * 29.97))
    assert maria[0].media_pool_item.name == "Caption Style A"
    assert tl.track("video", "SPEAKER_01").items[0].media_pool_item.name == "Caption Style B"
    assert 'Text+ track 2 "Maria" with 2 clips' in backend.messages[0][1]
    assert not os.path.exists(outcome.work_dir)


def test_full_flow_both_modes_and_name_collisions(config):
    resolve = make_world()
    tl = resolve.project.timeline
    tl.SetTrackName("video", 1, "Maria")  # an existing track already uses the speaker's name
    backend = FakeBackend(accept_all(names={"SPEAKER_01": "Maria"}, subtitles=True, textplus=True))
    outcome = _run(resolve, backend, config)
    assert outcome.error is None, backend.errors
    assert [t.name for t in tl.tracks["subtitle"]] == ["Maria", "Maria 2"]
    assert [t.name for t in tl.tracks["video"]] == ["Maria", "Maria 2", "Maria 3"]
    assert len(tl.tracks["video"][0].items) == 1  # existing "Maria" track untouched
    assert len(outcome.created) == 4


def test_subtitle_fallback_keeps_srts_and_tells_the_user(config):
    resolve = make_world()
    tl = resolve.project.timeline
    tl.subtitle_append_ignores_record_frame = True
    backend = FakeBackend(accept_all(subtitles=True, textplus=True))
    outcome = _run(resolve, backend, config)
    assert outcome.error is None, backend.errors
    assert tl.GetTrackCount("subtitle") == 0
    assert [t.name for t in tl.tracks["video"]] == ["Video 1", "Maria", "SPEAKER_01"]  # Text+ still created
    assert len(outcome.subtitle_fallbacks) == 2
    summary = backend.messages[0][1]
    assert "could not place these SRT files" in summary and "File > Import > Subtitle" in summary
    for plan, _message in outcome.subtitle_fallbacks:
        assert plan.srt_path in summary and os.path.isfile(plan.srt_path)
    assert outcome.kept_work_dir and os.path.isdir(outcome.work_dir)
    assert "Files kept in" in summary


def test_cancel_in_review_creates_nothing_and_cleans_up(config):
    resolve = make_world()
    backend = FakeBackend(answer=None)
    outcome = _run(resolve, backend, config)
    tl = resolve.project.timeline
    assert outcome.cancelled and outcome.created == []
    assert tl.GetTrackCount("video") == 1 and tl.GetTrackCount("subtitle") == 0
    assert backend.messages == [] and backend.errors == []
    assert not os.path.exists(outcome.work_dir)


def test_review_with_no_outputs_selected_is_a_cancel(config):
    backend = FakeBackend(ReviewResult(subtitles=False, textplus=False))
    outcome = _run(make_world(), backend, config)
    assert outcome.cancelled and not backend.messages


def test_cancel_during_engine_run(config):
    resolve = make_world()
    config["engine_args"] = ["--slow"]
    backend = FakeBackend(accept_all(), cancel_when=lambda view: "Transcribing…" in view.statuses)
    outcome = _run(resolve, backend, config)
    assert outcome.cancelled and outcome.created == [] and not backend.errors
    assert resolve.project.timeline.GetTrackCount("subtitle") == 0


def test_engine_failure_is_reported_not_raised(config):
    config["engine_args"] = ["--fail"]
    backend = FakeBackend(accept_all())
    outcome = _run(make_world(), backend, config)
    assert outcome.error and "no speech found" in outcome.error
    assert backend.errors == [("Better Captions", outcome.error)]
    assert backend.requests == []


def test_missing_timeline_is_reported(config):
    resolve = make_world()
    resolve.project.timeline = None
    backend = FakeBackend(accept_all())
    outcome = _run(resolve, backend, config)
    assert "timeline" in outcome.error and backend.errors


def test_missing_config_is_reported(tmp_path):
    backend = FakeBackend(accept_all())
    outcome = app.main(resolve=make_world(), backend=backend, environ={"BETTER_CAPTIONS_HOME": str(tmp_path)})
    assert "installer" in outcome.error and backend.errors


def test_textplus_without_templates_in_media_pool(config):
    resolve = make_world()
    resolve.project.media_pool.root.subfolders = []  # no templates anywhere
    backend = FakeBackend(accept_all(subtitles=True, textplus=True))
    outcome = _run(resolve, backend, config)
    assert backend.requests[0].templates == []
    assert outcome.error is None
    assert [t.name for t in resolve.project.timeline.tracks["video"]] == ["Video 1"]
    assert any("no Text+ template chosen" in w for w in outcome.warnings)


def test_textplus_template_without_text_node_is_a_warning(config):
    resolve = make_world()
    mp = resolve.project.media_pool
    mp.root.clips.insert(0, FakeMediaPoolItem("Background only", kind="Generator", has_textplus=False))
    backend = FakeBackend(accept_all(subtitles=False, textplus=True, template_index={"SPEAKER_00": 0, "SPEAKER_01": 1}))
    outcome = _run(resolve, backend, config)
    tl = resolve.project.timeline
    assert outcome.error is None
    assert [t.name for t in tl.tracks["video"]] == ["Video 1", "SPEAKER_01"]
    assert any("Maria not created" in w and "no Text+ node" in w for w in outcome.warnings)
    assert "Warning:" in backend.messages[0][1]


def test_unexpected_exception_is_shown_with_traceback(config):
    resolve = make_world()
    resolve.project.media_pool.GetRootFolder = lambda: (_ for _ in ()).throw(KeyError("boom"))
    backend = FakeBackend(accept_all())
    outcome = _run(resolve, backend, config)
    assert outcome.error.startswith("KeyError")
    assert "Traceback" in backend.errors[0][1]
