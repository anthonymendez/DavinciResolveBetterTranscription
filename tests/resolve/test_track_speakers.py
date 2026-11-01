"""Pinning an audio track to a named speaker: the picker's speaker names, remembering them, rendering
each pinned track alone, passing it to the engine as --fixed-track, and the review pre-filling the name."""

import json
import os
import sys

import pytest

from better_captions_plugin import app, track_picker
from better_captions_plugin import config as config_mod
from tests.resolve.fake_resolve import FakeTrack, make_world
from tests.resolve.fake_ui import FakeBackend, accept_all
from tests.resolve.test_ui import FakeBmd, FakeFusion

FAKE_ENGINE = os.path.join(os.path.dirname(__file__), "fake_engine.py")
TRACKS = [(1, "Dialogue", True), (2, "Microphone", True), (3, "Game", False)]


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{}")
    return {
        "engine": [sys.executable, "-I", FAKE_ENGINE],
        "model": "fake-model",
        "naming_provider": "heuristic",
        "_path": str(path),
    }


def _saved(config):
    with open(config["_path"], encoding="utf-8") as handle:
        return json.load(handle)


# --- TrackPick, config


def test_a_track_pick_is_the_list_of_indices_with_speaker_names_for_chosen_tracks_only():
    pick = track_picker.TrackPick([1, 2], {1: "  Anthony ", 2: "", 3: "Sam"})
    assert pick == [1, 2] and pick.speakers == {1: "Anthony"}
    assert track_picker.TrackPick([1], {1: "A=B"}).speakers == {1: "A B"}  # "=" would split NAME=PATH
    assert track_picker.speakers_of([1, 2]) == {} and track_picker.speakers_of(None) == {}


def test_speaker_names_are_remembered_per_timeline(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"model": "small"}))
    config = config_mod.load_config(str(path))
    config_mod.remember_audio_tracks(config, "Timeline 1", [1, 2], speakers={2: "Anthony"})
    config_mod.remember_audio_tracks(config, "Timeline 2", [1], speakers={1: "Sam"})

    reloaded = config_mod.load_config(str(path))
    assert reloaded["model"] == "small"
    assert config_mod.remembered_track_speakers(reloaded, "Timeline 1") == {2: "Anthony"}
    pick = track_picker.default_selection(reloaded, "Timeline 1", TRACKS)
    assert pick == [1, 2] and pick.speakers == {2: "Anthony"}
    assert track_picker.default_selection(reloaded, "Timeline 2", TRACKS).speakers == {1: "Sam"}
    assert track_picker.default_selection(reloaded, "Other", TRACKS).speakers == {}

    config_mod.remember_audio_tracks(reloaded, "Timeline 1", [1, 2], speakers={})  # names cleared
    again = config_mod.load_config(str(path))
    assert config_mod.remembered_track_speakers(again, "Timeline 1") == {}
    assert config_mod.remembered_track_speakers(again, "Timeline 2") == {1: "Sam"}


def test_remembered_speakers_ignore_malformed_config():
    for value in (None, [], "x", {"T": "Anthony"}, {"T": {"x": "A", "0": "B", "2": 5, "3": "  "}}):
        assert config_mod.remembered_track_speakers({config_mod.TRACK_SPEAKERS: value}, "T") == {}


def test_no_speakers_ever_named_leaves_no_track_speakers_key(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{}")
    config = config_mod.load_config(str(path))
    config_mod.remember_audio_tracks(config, "T", [1], speakers={})
    assert config_mod.TRACK_SPEAKERS not in json.loads(path.read_text())


def test_engine_command_adds_a_fixed_track_per_pinned_speaker(tmp_path):
    engine = tmp_path / "python"
    engine.write_text("")
    argv = config_mod.engine_command(
        {"engine": [str(engine)]}, "mix.wav", "out", fixed_tracks=[("Anthony", "/a/mic.wav"), ("Sam", "s.wav")]
    )
    assert argv[argv.index("--fixed-track") :] == ["--fixed-track", "Anthony=/a/mic.wav", "--fixed-track", "Sam=s.wav"]
    assert "--fixed-track" not in config_mod.engine_command({"engine": [str(engine)]}, "mix.wav", "out")


# --- dialogs


def _uimanager_pick(script, preselected):
    bmd = FakeBmd(script)
    fusion = FakeFusion()
    dispatcher = bmd.UIDispatcher(fusion.UIManager)
    return track_picker.pick_audio_tracks_uimanager(fusion, fusion.UIManager, dispatcher, TRACKS, preselected)


def test_uimanager_picker_fills_and_returns_speaker_names():
    seen = {}

    def script(window):
        items = window.GetItems()
        seen["start"] = [items["Speaker_%d" % i].Text for i in (1, 2, 3)]
        seen["placeholder"] = items["Speaker_1"].props.get("PlaceholderText")
        items["Speaker_2"].Text = "Anthony "
        items["Speaker_3"].Text = "Sam"  # not ticked: not a pinned track
        window.On.Ok.Clicked()

    result = _uimanager_pick(script, track_picker.TrackPick([1, 2], {1: "Maria"}))
    assert seen["start"] == ["Maria", "", ""]
    assert seen["placeholder"] == track_picker.SPEAKER_PLACEHOLDER
    assert isinstance(result, track_picker.TrackPick)
    assert result == [1, 2] and result.speakers == {1: "Maria", 2: "Anthony"}


def test_uimanager_picker_accepts_a_plain_preselection():
    result = _uimanager_pick(lambda window: window.On.Ok.Clicked(), [1, 3])
    assert result == [1, 3] and result.speakers == {}


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


def test_tk_picker_fills_and_returns_speaker_names(tk_root):
    dialog = track_picker._TkTrackDialog(tk_root, TRACKS, track_picker.TrackPick([1, 2], {1: "Maria"}))
    assert [dialog.speaker_vars[i].get() for i in (1, 2, 3)] == ["Maria", "", ""]
    dialog.speaker_vars[2].set("Anthony")
    dialog._ok()
    assert dialog.result == [1, 2] and dialog.result.speakers == {1: "Maria", 2: "Anthony"}


# --- the flow


def _world():
    """A timeline with Dialogue (track 1) and Microphone (track 2)."""
    resolve = make_world()
    timeline = resolve.GetProjectManager().GetCurrentProject().GetCurrentTimeline()
    timeline.tracks["audio"].append(FakeTrack("Microphone"))
    return resolve, timeline


def _spy(monkeypatch):
    """Record the enabled audio tracks of every render, and every engine command line."""
    renders, commands = [], []
    real_render = app.resolve_io.render_audio
    real_command = config_mod.engine_command

    def render(project, tl, *args, **kwargs):
        renders.append(tl.enabled_audio())
        return real_render(project, tl, *args, **kwargs)

    def command(*args, **kwargs):
        argv = real_command(*args, **kwargs)
        audio = {}
        for spec in _tracks(argv):
            who, _sep, path = spec.partition("=")
            with open(path, "rb") as handle:  # the work dir is gone after the run
                audio[who.partition(":")[2]] = handle.read()
        commands.append((argv, audio))
        return argv

    monkeypatch.setattr(app.resolve_io, "render_audio", render)
    monkeypatch.setattr(app.config_mod, "engine_command", command)
    return renders, commands


def _tracks(argv):
    return [argv[i + 1] for i, arg in enumerate(argv) if arg == "--track"]


def _who(argv):
    """ROLE:NAME of each --track."""
    return [spec.partition("=")[0] for spec in _tracks(argv)]


def test_a_pinned_track_is_rendered_alone_and_passed_to_the_engine(config, monkeypatch):
    resolve, timeline = _world()
    renders, commands = _spy(monkeypatch)
    backend = FakeBackend(accept_all(subtitles=True), tracks_answer=[1, 2], speakers_answer={2: "Anthony"})
    outcome = app.main(resolve=resolve, backend=backend, config=config, sleep=lambda s: None)
    assert outcome.error is None and not outcome.cancelled

    assert renders == [[1], [2]]  # each track alone, no mix
    assert timeline.enabled_audio() == [1, 2]  # put back as it was
    argv, audio = commands[0]
    assert argv[argv.index("run") + 1] == "--out"  # no mix to pass
    assert _who(argv) == ["group:Audio 1", "person:Anthony"]
    assert audio["Anthony"].endswith(b"tracks=2") and audio["Audio 1"].endswith(b"tracks=1")
    assert _saved(config)[config_mod.TRACK_ROLES] == {
        "Timeline 1": {"1": {"role": "group"}, "2": {"role": "person", "name": "Anthony"}}
    }


def test_the_review_pre_fills_the_pinned_speakers_name(config, monkeypatch):
    resolve, _timeline = _world()
    backend = FakeBackend(accept_all(subtitles=True), tracks_answer=[1, 2], speakers_answer={2: "Anthony"})
    outcome = app.main(resolve=resolve, backend=backend, config=config, sleep=lambda s: None)
    assert outcome.error is None
    rows = {row.id: row for row in backend.requests[0].speakers}
    assert (rows["PINNED_00"].name, rows["PINNED_00"].source) == ("Anthony", "user")
    assert any(result.plan.name == "Anthony" for result in outcome.created)


def test_the_next_run_remembers_the_name_and_reuses_the_single_track_render(config, monkeypatch):
    resolve, _timeline = _world()
    first = FakeBackend(accept_all(subtitles=True), tracks_answer=[1, 2], speakers_answer={2: "Anthony"})
    assert app.main(resolve=resolve, backend=first, config=config, sleep=lambda s: None).error is None

    renders, commands = _spy(monkeypatch)
    second = FakeBackend(accept_all(subtitles=True))  # keeps the preselection, names included
    outcome = app.main(resolve=resolve, backend=second, config=config, sleep=lambda s: None)
    assert outcome.error is None
    assert second.track_requests[0][1] == [1, 2]
    assert second.track_speaker_requests == [{2: "Anthony"}]
    assert renders == []  # both one-track renders came from the render cache
    argv, audio = commands[0]
    assert _who(argv) == ["group:Audio 1", "person:Anthony"]
    assert audio["Anthony"].endswith(b"tracks=2")


def test_tracks_without_a_speaker_name_are_several_people_each_rendered_alone(config, monkeypatch):
    resolve, _timeline = _world()
    renders, commands = _spy(monkeypatch)
    backend = FakeBackend(accept_all(subtitles=True), tracks_answer=[1, 2])
    outcome = app.main(resolve=resolve, backend=backend, config=config, sleep=lambda s: None)
    assert outcome.error is None
    assert renders == [[1], [2]]
    assert _who(commands[0][0]) == ["group:Audio 1", "group:Microphone"]
    assert config_mod.TRACK_SPEAKERS not in _saved(config)


def test_two_pinned_tracks_each_render_alone(config, monkeypatch):
    resolve, timeline = _world()
    timeline.tracks["audio"].append(FakeTrack("Guest mic"))
    renders, commands = _spy(monkeypatch)
    backend = FakeBackend(accept_all(subtitles=True), tracks_answer=[1, 2, 3], speakers_answer={3: "Sam", 2: "Anthony"})
    outcome = app.main(resolve=resolve, backend=backend, config=config, sleep=lambda s: None)
    assert outcome.error is None
    assert renders == [[1], [2], [3]]
    argv, audio = commands[0]
    assert _who(argv) == ["group:Audio 1", "person:Anthony", "person:Sam"]
    assert audio["Anthony"].endswith(b"tracks=2") and audio["Sam"].endswith(b"tracks=3")


def test_old_track_speakers_are_read_as_person_tracks_and_moved_to_track_roles(config, monkeypatch):
    with open(config["_path"], "w", encoding="utf-8") as handle:
        json.dump({config_mod.TRACK_SPEAKERS: {"Timeline 1": {"2": "Anthony"}, "Other": {"1": "Sam"}}}, handle)
    config[config_mod.TRACK_SPEAKERS] = {"Timeline 1": {"2": "Anthony"}, "Other": {"1": "Sam"}}
    resolve, _timeline = _world()
    _renders, commands = _spy(monkeypatch)
    backend = FakeBackend(accept_all(subtitles=True))
    assert app.main(resolve=resolve, backend=backend, config=config, sleep=lambda s: None).error is None
    assert backend.track_role_requests[0] == {1: "group", 2: "person"}
    assert _who(commands[0][0]) == ["group:Audio 1", "person:Anthony"]
    saved = _saved(config)
    assert saved[config_mod.TRACK_SPEAKERS] == {"Other": {"1": "Sam"}}
    assert saved[config_mod.TRACK_ROLES]["Timeline 1"]["2"] == {"role": "person", "name": "Anthony"}
