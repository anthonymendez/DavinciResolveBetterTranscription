"""resolve_io against the fake object model."""

import os

import pytest

from better_captions_plugin import resolve_io
from better_captions_plugin.planning import ClipPlacement, TrackPlan
from better_captions_plugin.resolve_io import ResolveError, SubtitlePlacementError
from better_captions_plugin.timing import TimelineClock
from tests.resolve.fake_resolve import (
    FakeMediaPoolItem,
    FakeProject,
    FakeTimeline,
    make_media_pool,
    make_world,
)


def _srt(path, cues):
    def ts(s):
        ms = int(round(s * 1000))
        return "%02d:%02d:%02d,%03d" % (ms // 3600000, ms // 60000 % 60, ms // 1000 % 60, ms % 1000)

    path.write_text(
        "\n".join("%d\n%s --> %s\n%s\n" % (i, ts(a), ts(b), t) for i, (a, b, t) in enumerate(cues, 1)), encoding="utf-8"
    )
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


def _render(project, tmp_path, start=86400, end=None, **kwargs):
    kwargs.setdefault("sleep", lambda s: None)
    return resolve_io.render_audio(project, project.timeline, str(tmp_path / "audio"), start, end, **kwargs)


def test_render_audio_uses_the_audio_only_preset_and_restores_the_render(tmp_path):
    """Real Resolve: Wave has no codecs, so the preset sets the render up (not SetCurrentRenderFormatAndCodec)."""
    project = make_world().project
    pumps = []
    wav = _render(project, tmp_path, pump=lambda: pumps.append(1))
    assert os.path.isfile(wav) and wav.endswith(".wav") and pumps
    job = project.last_job
    assert job["format"] == ("wav", "")  # what the Audio Only preset selects
    settings = job["settings"]
    assert settings["ExportVideo"] is False and settings["ExportAudio"] is True
    timeline = project.timeline
    assert settings["SelectAllFrames"] is False  # an explicit range: a stale one can block SelectAllFrames
    assert (settings["MarkIn"], settings["MarkOut"]) == (timeline.GetStartFrame(), timeline.GetEndFrame() - 1)
    assert settings["AudioCodec"] == "lpcm" and "AudioFormat" not in settings  # pre-21.1 rejects AudioFormat
    assert any("AudioFormat" in call for call in project.settings_calls)  # ... but it was tried first
    assert project.jobs == {} and project.deleted_jobs == ["job-1"]
    # The user's render is put back: format/codec, video export, mode.
    assert project.render_format == ("mov", "H264")
    assert project.render_settings["ExportVideo"] is True and project.render_mode == 0


def test_render_audio_on_21_1_asks_for_wav_directly(tmp_path):
    project = make_world().project
    project.audio_render_api = True
    project.presets = []  # even without the preset
    wav = _render(project, tmp_path)
    assert wav.endswith(".wav")
    settings = project.last_job["settings"]
    assert settings["AudioFormat"] == "wav" and settings["AudioCodec"] == "lpcm"


def test_render_audio_with_range_sets_marks(tmp_path):
    project = make_world().project
    _render(project, tmp_path, 86500, 86700)
    settings = project.last_job["settings"]
    assert settings["SelectAllFrames"] is False
    assert (settings["MarkIn"], settings["MarkOut"]) == (86500, 86700)


def test_render_audio_finds_a_localised_audio_preset(tmp_path):
    project = make_world().project
    project.presets = ["H.264 Master", "Solo audio"]
    assert _render(project, tmp_path).endswith(".wav")
    assert project.last_job["format"] == ("wav", "")


def test_render_audio_without_any_way_to_get_a_wav_says_so(tmp_path):
    project = make_world().project
    project.presets = ["H.264 Master"]  # no audio preset, no 21.1 audio API, Wave takes no codec
    with pytest.raises(ResolveError, match="Audio Only"):
        _render(project, tmp_path)
    assert project.jobs == {} and project.render_format == ("mov", "H264")


def test_render_audio_reports_a_non_wav_render(tmp_path):
    project = make_world().project
    original = project.LoadRenderPreset

    def load_preset_as_mxf(name):
        ok = original(name)
        project.render_format = ("mxf", "")  # e.g. a preset left on MXF OP-Atom
        return ok

    project.formats["MXF OP-Atom"] = "mxf"
    project.LoadRenderPreset = load_preset_as_mxf
    with pytest.raises(ResolveError, match=r"\.mxf, not a WAV"):
        _render(project, tmp_path)


def test_render_audio_retries_a_start_that_did_not_take(tmp_path):
    project = make_world().project
    project.start_failures = 1
    assert _render(project, tmp_path).endswith(".wav")
    project = make_world().project
    project.start_failures = 2
    with pytest.raises(ResolveError, match="could not start"):
        _render(project, tmp_path)
    assert project.deleted_jobs == ["job-1"]


def test_render_audio_failures(tmp_path):
    project = make_world().project
    project.fail_render = True
    with pytest.raises(ResolveError, match="Failed"):
        _render(project, tmp_path)
    assert project.render_format == ("mov", "H264")


def test_render_audio_cancel(tmp_path):
    project = make_world().project
    project.polls_until_done = 50
    with pytest.raises(ResolveError, match="cancelled"):
        _render(project, tmp_path, should_cancel=lambda: True)
    assert project.deleted_jobs == ["job-1"]


def test_wav_codecs_use_the_extension_and_prefer_pcm():
    project = make_world().project
    project.formats = {"WAVE AUDIO": ".WAV"}
    project.codecs = {"WAV": {"AAC": "aac", "Linear PCM 16": "lpcm16"}}
    ext = resolve_io._wav_extension(project)
    assert ext == "WAV"
    assert resolve_io._wav_codecs(project, ext)[:2] == ["lpcm16", "aac"]
    project.codecs = {}
    assert resolve_io._wav_codecs(project, "wav") == ["lpcm", "LinearPCM"]


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
    clips = [
        ClipPlacement(86412, 36, "Hey Jordan,\nhow are you?", 0.5, 2.0),
        ClipPlacement(86496, 60, "Ready?", 4.0, 6.5),
    ]
    result = resolve_io.create_textplus_track(mp, tl, _plan("Maria", clips, template=template))
    assert (result.track_type, result.track_index, result.placed) == ("video", 2, 2)
    assert result.misplaced == [] and result.text_failures == []
    track = tl.track("video", "Maria")
    assert [(i.GetStart(), i.GetDuration(), i.text()) for i in track.items] == [
        (86412, 36, "Hey Jordan,\nhow are you?"),
        (86496, 60, "Ready?"),
    ]
    assert tl.tracks["video"][0].name == "Video 1" and len(tl.tracks["video"][0].items) == 1
    info = mp.append_calls[0][0]
    assert info == {
        "mediaPoolItem": template.item,
        "startFrame": 0,
        "endFrame": 35,
        "trackIndex": 2,
        "recordFrame": 86412,
        "mediaType": 1,
    }


def test_create_textplus_track_recovers_items_from_track_when_append_returns_nothing():
    resolve = make_world()
    tl, mp = resolve.project.timeline, resolve.project.media_pool
    mp.append_returns_items = False
    template = resolve_io.find_text_templates(mp)[0]
    result = resolve_io.create_textplus_track(
        mp, tl, _plan("S", [ClipPlacement(86400, 10, "a", 0, 0.4)], template=template)
    )
    assert result.placed == 1 and tl.track("video", "S").items[0].text() == "a"


def test_create_textplus_track_reports_duration_mismatch():
    resolve = make_world()
    tl, mp = resolve.project.timeline, resolve.project.media_pool
    quirky = FakeMediaPoolItem("Fixed length", kind="Generator", placed_duration=120)
    mp.root.clips.append(quirky)
    template = next(t for t in resolve_io.find_text_templates(mp) if t.name == "Fixed length")
    result = resolve_io.create_textplus_track(
        mp, tl, _plan("S", [ClipPlacement(86400, 10, "a", 0, 0.4)], template=template)
    )
    assert result.placed == 1 and result.misplaced == ["a: 120 frames instead of 10"]


def test_create_textplus_track_without_textplus_node_removes_track():
    resolve = make_world()
    tl, mp = resolve.project.timeline, resolve.project.media_pool
    bad = FakeMediaPoolItem("Not a title", kind="Generator", has_textplus=False)
    mp.root.clips.append(bad)
    template = next(t for t in resolve_io.find_text_templates(mp) if t.name == "Not a title")
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
    plan = _plan(
        "Maria",
        [ClipPlacement(86412, 36, "Hey", 0.5, 2.0), ClipPlacement(86496, 60, "Ready?", 4.0, 6.5)],
        srt_path=srt,
        mode="subtitles",
    )
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
        resolve_io.create_subtitle_track(
            mp, tl, _plan("M", [], srt_path=str(tmp_path / "missing.srt"), mode="subtitles"), 0
        )
    srt = _srt(tmp_path / "a.srt", [(0, 1, "x")])
    mp.ImportMedia = lambda paths: []
    with pytest.raises(SubtitlePlacementError, match="did not import"):
        resolve_io.create_subtitle_track(
            mp, tl, _plan("M", [ClipPlacement(0, 24, "x", 0, 1)], srt_path=srt, mode="subtitles"), 0
        )
    assert tl.GetTrackCount("subtitle") == 0


def test_whole_timeline_range_falls_back_to_select_all_frames():
    class Blind:
        def GetStartFrame(self):
            raise RuntimeError("no API")

        def GetEndFrame(self):
            return None

    assert resolve_io.whole_timeline_range(Blind(), 86400) == (86400, None)


def test_whole_timeline_render_starts_despite_a_stale_mark_range(tmp_path):
    project = make_world().project
    project.stale_range_blocks_select_all = True
    wav = resolve_io.render_audio(project, project.timeline, str(tmp_path), 86400, None, sleep=lambda s: None)
    assert os.path.isfile(wav)


def test_create_textplus_track_measures_an_exclusive_end_frame():
    # Resolve 21.1: endFrame is exclusive, so duration - 1 would make every clip a frame short
    resolve = make_world()
    tl, mp = resolve.project.timeline, resolve.project.media_pool
    mp.end_frame_exclusive = True
    template = resolve_io.find_text_templates(mp)[0]
    clips = [ClipPlacement(86400, 1, "um", 0, 0.01), ClipPlacement(86412, 36, "a", 0.5, 2.0)]
    clips.append(ClipPlacement(86496, 60, "b", 4.0, 6.5))
    result = resolve_io.create_textplus_track(mp, tl, _plan("S", clips, template=template))
    assert result.placed == 3 and result.misplaced == []
    assert [(i.GetStart(), i.GetDuration()) for i in tl.track("video", "S").items] == [
        (86400, 1),
        (86412, 36),
        (86496, 60),
    ]
    assert [i["endFrame"] for i in mp.append_calls[-1]] == [1, 36, 60]


def test_create_textplus_track_keeps_an_inclusive_probe():
    resolve = make_world()
    tl, mp = resolve.project.timeline, resolve.project.media_pool
    template = resolve_io.find_text_templates(mp)[0]
    clips = [ClipPlacement(86412, 36, "a", 0.5, 2.0), ClipPlacement(86496, 60, "b", 4.0, 6.5)]
    result = resolve_io.create_textplus_track(mp, tl, _plan("S", clips, template=template))
    assert result.placed == 2 and result.misplaced == []
    assert [[i["endFrame"] for i in call] for call in mp.append_calls] == [[35], [59]]


def test_create_textplus_track_places_the_rest_when_one_clip_fails_the_batch():
    resolve = make_world()
    tl, mp = resolve.project.timeline, resolve.project.media_pool
    template = resolve_io.find_text_templates(mp)[0]
    original = mp.AppendToTimeline

    def refuse_frame(infos):  # Resolve returns None for the whole call if it can't place one clip
        if any(i["recordFrame"] == 86600 for i in infos):
            if len(infos) > 1:
                return None
            return []
        return original(infos)

    mp.AppendToTimeline = refuse_frame
    clips = [ClipPlacement(86412, 36, "a", 0.5, 2.0), ClipPlacement(86496, 60, "b", 4.0, 6.5)]
    clips.append(ClipPlacement(86600, 10, "c", 8.0, 8.4))
    result = resolve_io.create_textplus_track(mp, tl, _plan("S", clips, template=template))
    assert result.placed == 2 and result.misplaced == ["c: no clip at frame 86600"]


def test_restore_marks_puts_back_marks_that_appending_cleared():
    marks = {"video": {"in": 86400, "out": 90000}, "audio": {"in": 86400, "out": 90000}}
    tl = FakeTimeline(mark_in_out=marks)
    saved = resolve_io.save_marks(tl)
    tl.mark_in_out = None
    resolve_io.restore_marks(tl, saved)
    assert tl.GetMarkInOut() == marks


def test_restore_marks_without_marks_or_api_does_nothing():
    tl = FakeTimeline()
    resolve_io.restore_marks(tl, resolve_io.save_marks(tl))
    assert tl.mark_in_out is None
    tl = FakeTimeline(has_mark_in_out_api=False)
    assert resolve_io.save_marks(tl) == {}
    resolve_io.restore_marks(tl, {"video": {"in": 1, "out": 2}})  # no SetMarkInOut either: no error


def test_render_range_reads_marks_counted_from_the_timeline_start():
    # Resolve 21.1: a range at 01:00:04:04 on a 01:00:00:00 timeline at 24 fps reads 100
    tl = FakeTimeline()
    clock = resolve_io.timeline_clock(tl, FakeProject(tl))
    tl.mark_in_out = {"video": {"in": 100, "out": 300}, "audio": {"in": 100, "out": 300}}
    assert resolve_io.render_range(tl, clock) == (86500, 86700)


def test_marks_offset_tells_relative_from_absolute():
    tl = FakeTimeline()
    tl.GetEndFrame = lambda: 86400 + 100000  # a long timeline: 86500 also fits as a relative mark
    assert resolve_io.marks_offset(tl, 86400, 100, 300) == 86400  # before the start: relative
    assert resolve_io.marks_offset(tl, 86400, 86500, 86700) == 86400  # fits inside counted from the start
    tl.GetEndFrame = lambda: 90000
    assert resolve_io.marks_offset(tl, 86400, 86500, 86700) == 0  # only fits as absolute frames
    assert resolve_io.marks_offset(tl, 0, 100, 300) == 0  # a timeline starting at 0: both readings agree


@pytest.mark.parametrize(("frame_rate", "start_tc"), [("23.976", "01:00:00:00"), ("29.97", "01:00:00;00")])
def test_create_textplus_track_converts_lengths_to_a_60_fps_template(frame_rate, start_tc):
    # Studio 21.1.2: a 60 fps "Fusion Title" on a slower timeline reads startFrame/endFrame in its own frames
    resolve = make_world(frame_rate=frame_rate, start_timecode=start_tc, drop_frame=";" in start_tc)
    tl, mp = resolve.project.timeline, resolve.project.media_pool
    mp.end_frame_exclusive = True
    template = resolve_io.find_text_templates(mp)[0]
    template.item.props["FPS"] = 60.0
    start = tl.start_frame
    clips = [ClipPlacement(start, 1, "um", 0, 0.04), ClipPlacement(start + 12, 15, "a", 0.5, 1.1)]
    clips.append(ClipPlacement(start + 40, 36, "b", 1.6, 3.1))
    result = resolve_io.create_textplus_track(mp, tl, _plan("S", clips, template=template))
    assert result.placed == 3 and result.misplaced == []
    assert [i.GetDuration() for i in tl.track("video", "S").items] == [1, 15, 36]


def test_source_frames_rounds_up_to_cover_the_length():
    assert resolve_io._source_frames(15, 60 / 23.976) == 38
    assert resolve_io._source_frames(1, 1.0) == 1
    assert resolve_io._source_frames(36, 1.0) == 36
