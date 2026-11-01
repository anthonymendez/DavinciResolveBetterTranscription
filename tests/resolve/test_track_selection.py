"""Choosing which audio tracks are rendered for transcription: listing, enabling, restoring, defaults, dialogs."""

import json

import pytest

from better_captions_plugin import config as config_mod
from better_captions_plugin import resolve_io, track_picker
from tests.resolve.fake_resolve import make_world
from tests.resolve.test_ui import FakeBmd, FakeFusion


def _timeline(enabled=(True, True, True), names=("Dialogue", "Music", "Game")):
    timeline = make_world().project.GetCurrentTimeline()
    timeline.tracks["audio"] = []
    for name, on in zip(names, enabled, strict=True):
        timeline.AddTrack("audio")
        timeline.SetTrackName("audio", len(timeline.tracks["audio"]), name)
        timeline.tracks["audio"][-1].enabled = on
    return timeline


# --- resolve_io.audio_tracks


def test_audio_tracks_lists_index_name_and_enabled():
    timeline = _timeline(enabled=(True, False, True))
    assert resolve_io.audio_tracks(timeline) == [(1, "Dialogue", True), (2, "Music", False), (3, "Game", True)]


def test_audio_tracks_names_unnamed_tracks_and_reports_enabled_without_the_enable_api():
    timeline = _timeline(names=("", "Music", "Game"), enabled=(True, False, True))
    timeline.has_track_enable_api = False
    assert resolve_io.audio_tracks(timeline) == [(1, "Audio 1", True), (2, "Music", True), (3, "Game", True)]


def test_audio_tracks_is_empty_when_the_count_is_unreadable():
    class Broken:
        def GetTrackCount(self, track_type):
            raise RuntimeError("no")

    assert resolve_io.audio_tracks(Broken()) == []


# --- resolve_io.tracks_enabled


def test_only_the_chosen_tracks_are_enabled_inside_the_context():
    timeline = _timeline(enabled=(True, True, True))
    with resolve_io.tracks_enabled(timeline, [1, 3]):
        assert timeline.enabled_audio() == [1, 3]
    assert timeline.enabled_audio() == [1, 2, 3]


def test_a_chosen_track_that_was_disabled_is_enabled_and_disabled_again_after():
    timeline = _timeline(enabled=(False, True, False))
    with resolve_io.tracks_enabled(timeline, [1, 2]):
        assert timeline.enabled_audio() == [1, 2]
    assert timeline.enabled_audio() == [2]


def test_original_state_is_restored_after_an_exception():
    timeline = _timeline(enabled=(True, False, True))
    with pytest.raises(RuntimeError, match="render failed"), resolve_io.tracks_enabled(timeline, [2]):
        assert timeline.enabled_audio() == [2]
        raise RuntimeError("render failed")
    assert timeline.enabled_audio() == [1, 3]


def test_original_state_is_restored_after_a_cancel():
    timeline = _timeline()
    with pytest.raises(resolve_io.ResolveError, match="cancelled"), resolve_io.tracks_enabled(timeline, [1]):
        raise resolve_io.ResolveError("cancelled")  # what render_audio raises
    assert timeline.enabled_audio() == [1, 2, 3]


def test_a_track_resolve_will_not_switch_restores_the_ones_already_switched():
    timeline = _timeline()
    timeline.refuse_track_enable = [("audio", 3)]
    with pytest.raises(resolve_io.ResolveError, match="audio track 3"), resolve_io.tracks_enabled(timeline, [1]):
        pytest.fail("the block must not run")
    assert timeline.enabled_audio() == [1, 2, 3]


def test_tracks_that_already_match_are_not_touched():
    timeline = _timeline(enabled=(True, False, True))
    with resolve_io.tracks_enabled(timeline, [1, 3]):
        pass
    assert timeline.track_enable_calls == []


def test_empty_or_unknown_selection_is_rejected_without_changing_anything():
    timeline = _timeline()
    for selection, message in (([], "at least one"), ([1, 7], "no audio track 7")):
        with pytest.raises(resolve_io.ResolveError, match=message), resolve_io.tracks_enabled(timeline, selection):
            pytest.fail("the block must not run")
    assert timeline.track_enable_calls == [] and timeline.enabled_audio() == [1, 2, 3]


def test_no_selection_or_no_enable_api_leaves_the_timeline_alone():
    timeline = _timeline()
    with resolve_io.tracks_enabled(timeline, None):
        pass
    timeline.has_track_enable_api = False
    with resolve_io.tracks_enabled(timeline, [1]):  # an older Resolve: every track is rendered
        pass
    assert timeline.track_enable_calls == [] and timeline.enabled_audio() == [1, 2, 3]
    assert not resolve_io.track_enable_supported(timeline)


def test_the_render_sees_only_the_chosen_tracks(tmp_path):
    world = make_world(profile="21.1.2-studio")
    project = world.project
    timeline = project.GetCurrentTimeline()
    timeline.AddTrack("audio")
    seen = []
    original_start = project.StartRendering

    def start(job_ids=None, is_interactive=False):
        seen.append(timeline.enabled_audio())
        return original_start(job_ids, is_interactive)

    project.StartRendering = start
    with resolve_io.tracks_enabled(timeline, [2]):
        resolve_io.render_audio(project, timeline, str(tmp_path), 0, None, sleep=lambda _s: None)
    assert seen == [[2]] and timeline.enabled_audio() == [1, 2]


# --- default selection and remembering

TRACKS = [(1, "Dialogue", True), (2, "Music", False), (3, "Game", True)]


def test_default_selection_is_the_currently_enabled_tracks():
    assert track_picker.default_selection({}, "Timeline 1", TRACKS) == [1, 3]


def test_default_selection_with_nothing_enabled_is_every_track():
    tracks = [(1, "A", False), (2, "B", False)]
    assert track_picker.default_selection({}, "Timeline 1", tracks) == [1, 2]


def test_default_selection_remembers_the_last_choice_per_timeline(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"model": "small"}))
    config = config_mod.load_config(str(path))
    assert config_mod.remember_audio_tracks(config, "Timeline 1", [2, 1]) is True
    config_mod.remember_audio_tracks(config, "Timeline 2", [3])

    reloaded = config_mod.load_config(str(path))
    assert reloaded["model"] == "small"
    assert track_picker.default_selection(reloaded, "Timeline 1", TRACKS) == [1, 2]
    assert track_picker.default_selection(reloaded, "Timeline 2", TRACKS) == [3]
    assert track_picker.default_selection(reloaded, "Other", TRACKS) == [1, 3]  # never picked: the enabled ones


def test_remembered_tracks_that_no_longer_exist_are_dropped():
    config = {config_mod.AUDIO_TRACKS: {"T": [2, 9]}}
    assert track_picker.default_selection(config, "T", TRACKS) == [2]
    config = {config_mod.AUDIO_TRACKS: {"T": [8, 9]}}
    assert track_picker.default_selection(config, "T", TRACKS) == [1, 3]


def test_remembered_tracks_ignore_malformed_config():
    for value in (None, [], "x", {"T": "12"}, {"T": [True, "a", 0, -1]}, {"T": []}):
        assert config_mod.remembered_audio_tracks({config_mod.AUDIO_TRACKS: value}, "T") is None


# --- dialogs (no window is ever shown: UIManager runs against the fake, Tk only with a display)


def _uimanager_pick(script, tracks=TRACKS, preselected=(1, 3)):
    bmd = FakeBmd(script)
    fusion = FakeFusion()
    dispatcher = bmd.UIDispatcher(fusion.UIManager)
    result = track_picker.pick_audio_tracks_uimanager(fusion, fusion.UIManager, dispatcher, tracks, preselected)
    return result, dispatcher.windows[-1]


def test_uimanager_picker_starts_with_the_preselection_and_returns_the_ticked_tracks():
    seen = {}

    def script(window):
        items = window.GetItems()
        seen["start"] = [items["Track_%d" % i].Checked for i in (1, 2, 3)]
        seen["texts"] = [items["Track_%d" % i].Text for i in (1, 2, 3)]
        items["Track_2"].Checked = True
        items["Track_1"].Checked = False
        # Music and Game start as "Not speech"; at least one ticked track must have speech
        items["Role_2"].CurrentIndex = track_picker.ROLES.index(track_picker.ROLE_GROUP)
        window.On.Ok.Clicked()

    result, window = _uimanager_pick(script)
    assert result == [2, 3]
    assert result.roles == {2: track_picker.ROLE_GROUP, 3: track_picker.ROLE_NONE}
    assert seen["start"] == [True, False, True]
    assert seen["texts"] == ["1  Dialogue", "2  Music  (disabled now)", "3  Game"]
    assert window.shown is False


def test_uimanager_picker_cancel_and_close_return_none():
    assert _uimanager_pick(lambda window: window.On.Cancel.Clicked())[0] is None
    assert _uimanager_pick(lambda window: window.On[track_picker.WINDOW_ID_TRACKS].Close())[0] is None


def test_uimanager_picker_refuses_an_empty_selection():
    seen = {}

    def script(window):
        items = window.GetItems()
        for i in (1, 3):
            items["Track_%d" % i].Checked = False
        window.On.Ok.Clicked()
        seen["status"] = items["TracksStatus"].Text
        window.On.Cancel.Clicked()

    result, _window = _uimanager_pick(script)
    assert result is None and seen["status"] == track_picker.NONE_PICKED


@pytest.fixture
def tk_root():
    from tests.resolve.conftest import skip_unless_gui_tests

    skip_unless_gui_tests()
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip("no display for tkinter: %s" % exc)
    root.withdraw()
    yield root
    try:
        root.destroy()
    except tk.TclError:
        pass


def test_tk_picker_round_trip(tk_root):
    dialog = track_picker._TkTrackDialog(tk_root, TRACKS, [1, 3])
    assert [dialog.vars[i].get() for i in (1, 2, 3)] == [True, False, True]
    dialog.vars[2].set(True)
    dialog.vars[1].set(False)
    dialog._ok()
    assert dialog.result == [2, 3]


def test_tk_picker_refuses_empty_and_cancels(tk_root):
    dialog = track_picker._TkTrackDialog(tk_root, TRACKS, [])
    dialog._ok()
    assert dialog.result is None and dialog.status_var.get() == track_picker.NONE_PICKED
    dialog._cancel()
    assert dialog.result is None
