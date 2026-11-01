"""Who talks on each audio track: the picker's roles (one person, several people, not speech), remembering
them per timeline, rendering every speech track alone (no mix) and passing it as --track ROLE:NAME=PATH."""

import json
import os
import sys

import pytest

from better_captions_plugin import app, track_picker
from better_captions_plugin import config as config_mod
from better_captions_plugin.track_picker import ROLE_GROUP, ROLE_NONE, ROLE_PERSON, ROLES, TrackPick
from tests.resolve.fake_resolve import FakeTrack, make_world
from tests.resolve.fake_ui import FakeBackend, accept_all
from tests.resolve.test_ui import FakeBmd, FakeFusion

FAKE_ENGINE = os.path.join(os.path.dirname(__file__), "fake_engine.py")
TRACKS = [(1, "Microphone", True), (2, "Discord", True), (3, "Game audio", True)]


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


# --- defaults


@pytest.mark.parametrize(
    "name, speaker, role",
    [
        ("Microphone", "Anthony", ROLE_PERSON),
        ("Game", "Anthony", ROLE_PERSON),  # a typed name wins
        ("Discord", "", ROLE_GROUP),
        ("Audio 1", "", ROLE_GROUP),
        ("Game audio", "", ROLE_NONE),
        ("Gameplay", "", ROLE_NONE),
        ("MUSIC", "", ROLE_NONE),
        ("bg-music 2", "", ROLE_NONE),
        ("SFX", "", ROLE_NONE),
        ("Friends", "", ROLE_GROUP),
        ("", "", ROLE_GROUP),
    ],
)
def test_default_role_from_the_track_name_and_a_typed_name(name, speaker, role):
    assert track_picker.default_role(name, speaker) == role


def test_the_preselection_has_a_role_for_every_track():
    pick = track_picker.default_selection({}, "T", TRACKS)
    assert pick == [1, 2, 3]
    assert pick.roles == {1: ROLE_GROUP, 2: ROLE_GROUP, 3: ROLE_NONE}


def test_a_person_without_a_name_is_several_people():
    pick = TrackPick([1, 2], {2: "Sam"}, {1: ROLE_PERSON, 2: ROLE_PERSON, 3: "robot"})
    assert pick.roles == {1: ROLE_PERSON, 2: ROLE_PERSON}  # unknown roles are dropped
    assert pick.role_of(1) == ROLE_GROUP and pick.role_of(2) == ROLE_PERSON
    assert track_picker.complete(TRACKS, pick).roles == {1: ROLE_GROUP, 2: ROLE_PERSON}


def test_speech_tracks_name_each_track_and_leave_out_not_speech():
    pick = TrackPick([1, 2, 3], {1: "Anthony"})
    assert track_picker.speech_tracks(TRACKS, pick) == [(1, ROLE_PERSON, "Anthony"), (2, ROLE_GROUP, "Discord")]
    unnamed = [(1, "", True), (2, "A=B", True)]
    assert track_picker.speech_tracks(unnamed, [1, 2]) == [(1, ROLE_GROUP, "Audio 1"), (2, ROLE_GROUP, "A B")]


def test_problems_the_dialog_refuses():
    assert track_picker.problem(TRACKS, TrackPick([])) == track_picker.NONE_PICKED
    assert track_picker.problem(TRACKS, TrackPick([3])) == track_picker.NO_SPEECH
    assert track_picker.problem(TRACKS, TrackPick([1], roles={1: ROLE_PERSON})) == track_picker.NAME_MISSING % 1
    assert track_picker.problem(TRACKS, TrackPick([1, 3], {1: "Anthony"})) is None


# --- config


def test_roles_are_remembered_per_timeline(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"model": "small"}))
    config = config_mod.load_config(str(path))
    config_mod.remember_audio_tracks(
        config, "T1", [1, 2, 3], speakers={1: "Anthony"}, roles={1: ROLE_PERSON, 2: ROLE_GROUP, 3: ROLE_NONE}
    )
    config_mod.remember_audio_tracks(config, "T2", [2], roles={2: ROLE_NONE})

    reloaded = config_mod.load_config(str(path))
    assert reloaded["model"] == "small"
    assert config_mod.remembered_track_roles(reloaded, "T1") == {1: ROLE_PERSON, 2: ROLE_GROUP, 3: ROLE_NONE}
    assert config_mod.remembered_track_speakers(reloaded, "T1") == {1: "Anthony"}
    assert config_mod.remembered_track_roles(reloaded, "T2") == {2: ROLE_NONE}
    pick = track_picker.default_selection(reloaded, "T2", [(1, "Mic", True), (2, "Discord", True)])
    assert pick == [2] and pick.roles == {1: ROLE_GROUP, 2: ROLE_NONE}  # Discord was "not speech" last time
    assert config_mod.remembered_track_roles(reloaded, "Other") == {}


def test_remembered_roles_ignore_malformed_config():
    for value in (None, [], "x", {"T": "group"}, {"T": {"x": {"role": "group"}, "0": {"role": "group"}}}):
        assert config_mod.remembered_track_roles({config_mod.TRACK_ROLES: value}, "T") == {}
    odd = {"T": {"1": {"role": "robot", "name": 5}, "2": "group", "3": {"role": "none"}}}
    assert config_mod.remembered_track_roles({config_mod.TRACK_ROLES: odd}, "T") == {3: ROLE_NONE}
    assert config_mod.remembered_track_speakers({config_mod.TRACK_ROLES: odd}, "T") == {}


def test_old_track_speakers_read_as_person_roles():
    config = {config_mod.TRACK_SPEAKERS: {"T": {"2": "Anthony"}}}
    assert config_mod.remembered_track_roles(config, "T") == {2: ROLE_PERSON}
    assert config_mod.remembered_track_speakers(config, "T") == {2: "Anthony"}


def test_engine_command_passes_each_track_without_a_mix(tmp_path):
    engine = tmp_path / "python"
    engine.write_text("")
    argv = config_mod.engine_command(
        {"engine": [str(engine)], "model": "small"},
        None,
        "out",
        tracks=[(ROLE_PERSON, "Anthony", "/a/mic.wav"), (ROLE_GROUP, "Discord", "d.wav")],
    )
    assert argv[:4] == [str(engine), "run", "--out", "out"]
    assert argv[argv.index("--track") :] == ["--track", "person:Anthony=/a/mic.wav", "--track", "group:Discord=d.wav"]


# --- dialogs


def _uimanager_pick(script, preselected, tracks=TRACKS):
    bmd = FakeBmd(script)
    fusion = FakeFusion()
    dispatcher = bmd.UIDispatcher(fusion.UIManager)
    return track_picker.pick_audio_tracks_uimanager(fusion, fusion.UIManager, dispatcher, tracks, preselected)


def test_uimanager_picker_shows_and_returns_roles():
    seen = {}

    def script(window):
        items = window.GetItems()
        seen["choices"] = items["Role_1"].items
        seen["start"] = [ROLES[items["Role_%d" % i].CurrentIndex] for i in (1, 2, 3)]
        items["Speaker_1"].Text = "Anthony"
        window.On.Speaker_1.TextChanged()  # typing a name makes the track "One person"
        seen["after_typing"] = ROLES[items["Role_1"].CurrentIndex]
        window.On.Ok.Clicked()

    result = _uimanager_pick(script, [1, 2, 3])
    assert seen["choices"] == ["One person", "Several people", "Not speech"]
    assert seen["start"] == [ROLE_GROUP, ROLE_GROUP, ROLE_NONE]
    assert seen["after_typing"] == ROLE_PERSON
    assert result == [1, 2, 3] and result.speakers == {1: "Anthony"}
    assert result.roles == {1: ROLE_PERSON, 2: ROLE_GROUP, 3: ROLE_NONE}


def test_uimanager_picker_starts_from_remembered_roles():
    seen = {}

    def script(window):
        items = window.GetItems()
        seen["start"] = [ROLES[items["Role_%d" % i].CurrentIndex] for i in (1, 2, 3)]
        window.On.Ok.Clicked()

    preselected = TrackPick([1, 2], {1: "Anthony"}, {1: ROLE_PERSON, 2: ROLE_NONE, 3: ROLE_GROUP})
    result = _uimanager_pick(script, preselected)
    assert seen["start"] == [ROLE_PERSON, ROLE_NONE, ROLE_GROUP]
    assert result == [1, 2] and result.roles == {1: ROLE_PERSON, 2: ROLE_NONE}


@pytest.mark.parametrize(
    "setup, message",
    [
        (lambda items: setattr(items["Track_2"], "Checked", False), track_picker.NO_SPEECH),
        (
            lambda items: setattr(items["Role_2"], "CurrentIndex", ROLES.index(ROLE_PERSON)),
            track_picker.NAME_MISSING % 2,
        ),
    ],
    ids=["only-not-speech", "person-without-name"],
)
def test_uimanager_picker_refuses_a_pick_it_cannot_transcribe(setup, message):
    seen = {}

    def script(window):
        items = window.GetItems()
        items["Track_1"].Checked = False
        setup(items)
        window.On.Ok.Clicked()
        seen["status"] = items["TracksStatus"].Text
        window.On.Cancel.Clicked()

    assert _uimanager_pick(script, [1, 2, 3]) is None
    assert seen["status"] == message


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


def test_tk_picker_shows_and_returns_roles(tk_root):
    dialog = track_picker._TkTrackDialog(tk_root, TRACKS, track_picker.default_selection({}, "T", TRACKS))
    assert [dialog.role(i) for i in (1, 2, 3)] == [ROLE_GROUP, ROLE_GROUP, ROLE_NONE]
    dialog.speaker_vars[1].set("Anthony")
    assert dialog.role(1) == ROLE_PERSON
    dialog._ok()
    assert dialog.result.roles == {1: ROLE_PERSON, 2: ROLE_GROUP, 3: ROLE_NONE}


# --- the flow


def _world():
    """A timeline with Microphone (1), Discord (2) and Game audio (3)."""
    resolve = make_world()
    timeline = resolve.GetProjectManager().GetCurrentProject().GetCurrentTimeline()
    timeline.tracks["audio"] = [FakeTrack("Microphone"), FakeTrack("Discord"), FakeTrack("Game audio")]
    return resolve, timeline


def _spy(monkeypatch):
    renders, commands = [], []
    real_render = app.resolve_io.render_audio
    real_command = config_mod.engine_command

    def render(project, tl, *args, **kwargs):
        renders.append(tl.enabled_audio())
        return real_render(project, tl, *args, **kwargs)

    def command(*args, **kwargs):
        argv = real_command(*args, **kwargs)
        specs = [argv[i + 1] for i, arg in enumerate(argv) if arg == "--track"]
        audio = {}
        for spec in specs:
            with open(spec.partition("=")[2], "rb") as handle:  # the work dir is gone after the run
                audio[spec.partition("=")[0]] = handle.read()
        commands.append((argv, specs, audio))
        return argv

    monkeypatch.setattr(app.resolve_io, "render_audio", render)
    monkeypatch.setattr(app.config_mod, "engine_command", command)
    return renders, commands


def _run(resolve, backend, config):
    return app.main(resolve=resolve, backend=backend, config=config, sleep=lambda s: None)


def test_each_speech_track_is_rendered_alone_and_not_speech_is_skipped(config, monkeypatch):
    resolve, timeline = _world()
    renders, commands = _spy(monkeypatch)
    backend = FakeBackend(accept_all(subtitles=True), speakers_answer={1: "Anthony"}, tracks_answer=[1, 2, 3])
    outcome = _run(resolve, backend, config)
    assert outcome.error is None and not outcome.cancelled

    assert renders == [[1], [2]]  # no mix, and the game audio is never rendered
    assert timeline.enabled_audio() == [1, 2, 3]
    argv, specs, audio = commands[0]
    assert argv[argv.index("run") + 1] == "--out"
    assert [s.partition("=")[0] for s in specs] == ["person:Anthony", "group:Discord"]
    assert audio["person:Anthony"].endswith(b"tracks=1") and audio["group:Discord"].endswith(b"tracks=2")
    assert _saved(config)[config_mod.TRACK_ROLES] == {
        "Timeline 1": {
            "1": {"role": ROLE_PERSON, "name": "Anthony"},
            "2": {"role": ROLE_GROUP},
            "3": {"role": ROLE_NONE},
        }
    }
    rows = {row.id: row for row in backend.requests[0].speakers}
    assert (rows["PINNED_00"].name, rows["PINNED_00"].source) == ("Anthony", "user")


def test_the_next_run_starts_from_the_remembered_roles_and_reuses_the_renders(config, monkeypatch):
    resolve, _timeline = _world()
    first = FakeBackend(
        accept_all(subtitles=True), tracks_answer=[1, 2, 3], speakers_answer={1: "Anthony"}, roles_answer={3: "group"}
    )
    assert _run(resolve, first, config).error is None

    renders, commands = _spy(monkeypatch)
    second = FakeBackend(accept_all(subtitles=True))  # keeps the preselection
    assert _run(resolve, second, config).error is None
    assert second.track_role_requests == [{1: ROLE_PERSON, 2: ROLE_GROUP, 3: ROLE_GROUP}]
    assert renders == []  # all three one-track renders came from the render cache
    assert [s.partition("=")[0] for s in commands[0][1]] == ["person:Anthony", "group:Discord", "group:Game audio"]


def test_saved_speaker_fixes_are_keyed_by_the_track_renders(config, monkeypatch):
    resolve, _timeline = _world()
    keys = []
    real = app.reapply_saved_edits

    def spy(editor, paths, audio_sha):
        keys.append(audio_sha)
        return real(editor, paths, audio_sha)

    monkeypatch.setattr(app, "reapply_saved_edits", spy)
    for answer in ([1, 2], [1, 2], [2]):
        assert _run(resolve, FakeBackend(accept_all(subtitles=True), tracks_answer=answer), config).error is None
    assert keys[0] and keys[0] == keys[1] != keys[2]


def test_only_not_speech_tracks_is_an_error(config, monkeypatch):
    resolve, _timeline = _world()
    renders, _commands = _spy(monkeypatch)
    backend = FakeBackend(accept_all(subtitles=True), tracks_answer=[3])
    outcome = _run(resolve, backend, config)
    assert outcome.error and "speech" in outcome.error
    assert renders == []


def test_the_progress_status_names_the_track_an_engine_stage_is_on(config, monkeypatch):
    resolve, _timeline = _world()
    backend = FakeBackend(accept_all(subtitles=True), tracks_answer=[1, 2])
    real_run = app.engine_runner.run_engine

    def run_engine(argv, on_event, **kwargs):
        on_event({"event": "progress", "stage": "transcribe", "pct": 5.0, "track": "Discord"})
        return real_run(argv, on_event=on_event, **kwargs)

    monkeypatch.setattr(app.engine_runner, "run_engine", run_engine)
    assert _run(resolve, backend, config).error is None
    assert any("(Discord)" in status for status in backend.progress_views[0].statuses)
