"""resolve_io against the fake object model."""

import os

import pytest

from better_captions_plugin import resolve_io
from better_captions_plugin.planning import ClipPlacement, TrackPlan
from better_captions_plugin.resolve_io import ResolveError, SubtitlePlacementError
from better_captions_plugin.timing import TimelineClock

from tests.resolve.fake_resolve import FakeMediaPoolItem, FakeProject, FakeResolve, FakeTimeline, make_media_pool, make_world


def _srt(path, cues):
    def ts(s):
        ms = int(round(s * 1000))
        return "%02d:%02d:%02d,%03d" % (ms // 3600000, ms // 60000 % 60, ms // 1000 % 60, ms % 1000)

    path.write_text("\n".join("%d\n%s --> %s\n%s\n" % (i, ts(a), ts(b), t) for i, (a, b, t) in enumerate(cues, 1)), encoding="utf-8")
    return str(path)


# -- context


def test_current_project_and_timeline_errors():
    with pytest.raises(ResolveError, match="inside DaVinci Resolve"):
        resolve_io.current_project_and_timeline(None)
    resolve = make_world()
    resolve.project.timeline = None
    with pytest.raises(ResolveError, match="timeline"):
        resolve_io.current_project_and_timeline(resolve)
    resolve = make_world()
    project, timeline = resolve_io.current_project_and_timeline(resolve)
    assert project is resolve.project and timeline is project.timeline


def test_is_studio():
    assert resolve_io.is_studio(make_world(product="DaVinci Resolve Studio"))
    assert not resolve_io.is_studio(make_world(product="DaVinci Resolve"))
    assert not resolve_io.is_studio(None)


def test_timeline_clock_reads_fps_start_and_drop_frame():
    tl = FakeTimeline(frame_rate="29.97", start_timecode="01:00:00;00", drop_frame=True)
    clock = resolve_io.timeline_clock(tl)
    assert (clock.fps, clock.drop_frame, clock.start_frame) == (29.97, True, 107892)
    tl = FakeTimeline(frame_rate="24", start_timecode="01:00:00:00")
    assert resolve_io.timeline_clock(tl) == TimelineClock(fps=24.0, drop_frame=False, start_frame=86400)


def test_timeline_clock_falls_back_to_project_setting_and_start_timecode():
    tl = FakeTimeline(frame_rate="25", start_timecode="00:00:00:00")
    tl.settings["timelineFrameRate"] = ""
    project = FakeProject(tl)
    project.settings["timelineFrameRate"] = "25"
    tl.GetStartFrame = lambda: (_ for _ in ()).throw(RuntimeError("old API"))
    tl.start_timecode = "00:10:00:00"
    assert resolve_io.timeline_clock(tl, project) == TimelineClock(fps=25.0, drop_frame=False, start_frame=15000)
    project.settings["timelineFrameRate"] = ""
    with pytest.raises(ResolveError, match="frame rate"):
        resolve_io.timeline_clock(tl, project)


def test_render_range_uses_in_out_marks_when_set():
    tl = FakeTimeline(frame_rate="24", start_timecode="01:00:00:00")
    clock = resolve_io.timeline_clock(tl)
    assert resolve_io.render_range(tl, clock) == (86400, None)
    tl.mark_in_out = {"video": {"in": 86500, "out": 86700}, "audio": {"in": 86500, "out": 86700}}
    assert resolve_io.render_range(tl, clock) == (86500, 86700)
    tl.mark_in_out = {"audio": {"in": 86510, "out": 86600}}
    assert resolve_io.render_range(tl, clock) == (86510, 86600)
    tl.has_mark_in_out_api = False  # older Resolve
    assert resolve_io.render_range(tl, clock) == (86400, None)


# -- render


def test_render_audio_writes_wav_with_audio_only_settings(tmp_path):
    resolve = make_world()
    project = resolve.project
    pumps = []
    wav = resolve_io.render_audio(project, project.timeline, str(tmp_path / "audio"), 86400, None, pump=lambda: pumps.append(1), sleep=lambda s: None)
    assert os.path.isfile(wav) and wav.endswith(".wav")
    assert pumps
    settings = project.jobs or {}
    assert not settings  # job deleted afterwards
    assert project.deleted_jobs == ["job-1"]
    assert project.render_settings["ExportVideo"] is False
    assert project.render_settings["ExportAudio"] is True
    assert project.render_settings["SelectAllFrames"] is True
    assert "MarkIn" not in project.render_settings
    assert project.render_format == ("Wave", "LinearPCM")
    assert project.render_mode == 0  # restored


def test_render_audio_with_range_sets_marks(tmp_path):
    project = make_world().project
    resolve_io.render_audio(project, project.timeline, str(tmp_path), 86500, 86700, sleep=lambda s: None)
    assert project.render_settings["SelectAllFrames"] is False
    assert (project.render_settings["MarkIn"], project.render_settings["MarkOut"]) == (86500, 86700)


def test_render_audio_failures(tmp_path):
    project = make_world().project
    project.fail_render = True
    with pytest.raises(ResolveError, match="Failed"):
        resolve_io.render_audio(project, project.timeline, str(tmp_path), 0, None, sleep=lambda s: None)
    project = make_world().project
    project.formats = {"QuickTime": "mov"}
    with pytest.raises(ResolveError, match="WAV"):
        resolve_io.render_audio(project, project.timeline, str(tmp_path), 0, None, sleep=lambda s: None)


def test_render_audio_cancel(tmp_path):
    project = make_world().project
    project.polls_until_done = 50
    with pytest.raises(ResolveError, match="cancelled"):
        resolve_io.render_audio(project, project.timeline, str(tmp_path), 0, None, should_cancel=lambda: True, sleep=lambda s: None)
    assert project.deleted_jobs == ["job-1"]


def test_pick_wav_format_matches_by_extension_then_name():
    project = make_world().project
    project.formats = {"WAVE AUDIO": ".WAV", "MP4": "mp4"}
    project.codecs = {"WAVE AUDIO": {"AAC": "aac", "Linear PCM 16": "lpcm16", "24-bit": "lpcm24"}}
    assert resolve_io.pick_wav_format(project) == ("WAVE AUDIO", "lpcm16")
    project.formats = {"Wav thing": "weird"}
    project.codecs = {"Wav thing": {"Only": "x"}}
    assert resolve_io.pick_wav_format(project) == ("Wav thing", "x")


# -- media pool


def test_find_text_templates_recurses_all_bins_and_skips_media_and_timelines():
    media_pool = make_media_pool(FakeTimeline())
    templates = resolve_io.find_text_templates(media_pool)
    assert [(t.name, t.bin_path) for t in templates] == [
        ("Plain Text+", "Master/Templates"),
        ("Caption Style A", "Master/Templates/Lower Thirds"),
        ("Caption Style B", "Master/Templates/Lower Thirds"),
    ]
    assert templates[1].label == "Caption Style A  (Master/Templates/Lower Thirds)"


def test_looks_like_title_template_handles_dict_returning_api():
    item = FakeMediaPoolItem("T", kind="Fusion Title")
    item.GetClipProperty = lambda key=None: dict(item.props)  # some versions ignore the key
    assert resolve_io.looks_like_title_template(item)
    assert not resolve_io.looks_like_title_template(FakeMediaPoolItem("C", kind="Compound"))
    assert not resolve_io.looks_like_title_template(FakeMediaPoolItem("M", kind="Multicam"))


# -- tracks


def _plan(name, clips, template=None, srt_path=None, mode="textplus"):
    return TrackPlan(speaker_id="S", name=name, mode=mode, clips=clips, template=template, srt_path=srt_path)


def test_add_track_appends_and_names_without_touching_existing():
    tl = FakeTimeline()
    index = resolve_io.add_track(tl, "video", "Maria")
    assert index == 2 and tl.GetTrackName("video", 2) == "Maria" and tl.GetTrackName("video", 1) == "Video 1"
    tl.refuse_add_track = True
    with pytest.raises(ResolveError, match="refused"):
        resolve_io.add_track(tl, "subtitle", "x")


def test_create_textplus_track_places_clips_and_sets_text():
    resolve = make_world()
    tl, mp = resolve.project.timeline, resolve.project.media_pool
    template = resolve_io.find_text_templates(mp)[1]
    clips = [ClipPlacement(86412, 36, "Hey Jordan,\nhow are you?", 0.5, 2.0), ClipPlacement(86496, 60, "Ready?", 4.0, 6.5)]
    result = resolve_io.create_textplus_track(mp, tl, _plan("Maria", clips, template=template))
    assert (result.track_type, result.track_index, result.placed) == ("video", 2, 2)
    assert result.misplaced == [] and result.text_failures == []
    track = tl.track("video", "Maria")
    assert [(i.GetStart(), i.GetDuration(), i.text()) for i in track.items] == [(86412, 36, "Hey Jordan,\nhow are you?"), (86496, 60, "Ready?")]
    assert tl.tracks["video"][0].name == "Video 1" and len(tl.tracks["video"][0].items) == 1
    info = mp.append_calls[0][0]
    assert info == {"mediaPoolItem": template.item, "startFrame": 0, "endFrame": 35, "trackIndex": 2, "recordFrame": 86412, "mediaType": 1}


def test_create_textplus_track_recovers_items_from_track_when_append_returns_nothing():
    resolve = make_world()
    tl, mp = resolve.project.timeline, resolve.project.media_pool
    mp.append_returns_items = False
    template = resolve_io.find_text_templates(mp)[0]
    result = resolve_io.create_textplus_track(mp, tl, _plan("S", [ClipPlacement(86400, 10, "a", 0, 0.4)], template=template))
    assert result.placed == 1 and tl.track("video", "S").items[0].text() == "a"


def test_create_textplus_track_reports_duration_mismatch():
    resolve = make_world()
    tl, mp = resolve.project.timeline, resolve.project.media_pool
    quirky = FakeMediaPoolItem("Fixed length", kind="Generator", placed_duration=120)
    mp.root.clips.append(quirky)
    template = [t for t in resolve_io.find_text_templates(mp) if t.name == "Fixed length"][0]
    result = resolve_io.create_textplus_track(mp, tl, _plan("S", [ClipPlacement(86400, 10, "a", 0, 0.4)], template=template))
    assert result.placed == 1 and result.misplaced == ["a: 120 frames instead of 10"]


def test_create_textplus_track_without_textplus_node_removes_track():
    resolve = make_world()
    tl, mp = resolve.project.timeline, resolve.project.media_pool
    bad = FakeMediaPoolItem("Not a title", kind="Generator", has_textplus=False)
    mp.root.clips.append(bad)
    template = [t for t in resolve_io.find_text_templates(mp) if t.name == "Not a title"][0]
    with pytest.raises(ResolveError, match="no Text\\+ node"):
        resolve_io.create_textplus_track(mp, tl, _plan("S", [ClipPlacement(86400, 10, "a", 0, 0.4)], template=template))
    assert tl.GetTrackCount("video") == 1 and tl.deleted_tracks == [("video", 2, "S")]


def test_create_textplus_track_requires_template():
    resolve = make_world()
    with pytest.raises(ResolveError, match="template"):
        resolve_io.create_textplus_track(resolve.project.media_pool, resolve.project.timeline, _plan("S", []))


def test_set_textplus_text_falls_back_to_tool_list():
    from tests.resolve.fake_resolve import FakeComp, FakeTimelineItem, FakeTool

    tool = FakeTool("TextPlus", "Text1")
    comp = FakeComp([tool])
    comp.FindToolByID = lambda tool_id: None
    item = FakeTimelineItem("x", 0, 1, comps=[comp])
    resolve_io.set_textplus_text(item, "hello")
    assert tool.inputs["StyledText"] == "hello"
    with pytest.raises(ResolveError, match="no Fusion composition"):
        resolve_io.set_textplus_text(FakeTimelineItem("y", 0, 1), "x")


def test_create_subtitle_track_imports_srt_and_verifies(tmp_path):
    resolve = make_world()
    tl, mp = resolve.project.timeline, resolve.project.media_pool
    srt = _srt(tmp_path / "SPEAKER_00.srt", [(0.5, 2.0, "Hey"), (4.0, 6.5, "Ready?")])
    plan = _plan("Maria", [ClipPlacement(86412, 36, "Hey", 0.5, 2.0), ClipPlacement(86496, 60, "Ready?", 4.0, 6.5)], srt_path=srt, mode="subtitles")
    result = resolve_io.create_subtitle_track(mp, tl, plan, origin_frame=86400)
    assert (result.track_type, result.track_index, result.placed) == ("subtitle", 1, 2)
    track = tl.track("subtitle", "Maria")
    assert [(i.GetStart(), i.GetName()) for i in track.items] == [(86412, "Hey"), (86496, "Ready?")]
    assert mp.append_calls[0] == [{"mediaPoolItem": mp.imported[0], "trackIndex": 1, "recordFrame": 86400}]


def test_create_subtitle_track_falls_back_cleanly_when_resolve_ignores_record_frame(tmp_path):
    resolve = make_world()
    tl, mp = resolve.project.timeline, resolve.project.media_pool
    tl.subtitle_append_ignores_record_frame = True
    srt = _srt(tmp_path / "SPEAKER_00.srt", [(0.5, 2.0, "Hey")])
    plan = _plan("Maria", [ClipPlacement(86412, 36, "Hey", 0.5, 2.0)], srt_path=srt, mode="subtitles")
    with pytest.raises(SubtitlePlacementError, match="landed at frame"):
        resolve_io.create_subtitle_track(mp, tl, plan, origin_frame=86400)
    assert tl.GetTrackCount("subtitle") == 0  # the new track is gone
    assert mp.deleted == mp.imported  # and so is the imported SRT item
    assert os.path.isfile(srt)  # the file stays for manual import


def test_create_subtitle_track_missing_srt_or_import_failure(tmp_path):
    resolve = make_world()
    tl, mp = resolve.project.timeline, resolve.project.media_pool
    with pytest.raises(SubtitlePlacementError, match="no SRT"):
        resolve_io.create_subtitle_track(mp, tl, _plan("M", [], srt_path=str(tmp_path / "missing.srt"), mode="subtitles"), 0)
    srt = _srt(tmp_path / "a.srt", [(0, 1, "x")])
    mp.ImportMedia = lambda paths: []
    with pytest.raises(SubtitlePlacementError, match="did not import"):
        resolve_io.create_subtitle_track(mp, tl, _plan("M", [ClipPlacement(0, 24, "x", 0, 1)], srt_path=srt, mode="subtitles"), 0)
    assert tl.GetTrackCount("subtitle") == 0
