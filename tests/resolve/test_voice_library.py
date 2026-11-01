"""After the review, the plugin has the engine remember the voices of the speakers the editor confirmed by
name (``better-captions voices save``); a failure there is only a warning."""

import json
import os
import sys

import pytest

from better_captions_plugin import app, voice_library
from better_captions_plugin import config as config_mod
from tests.resolve.fake_engine import TRANSCRIPT, VOICES_FAIL, VOICES_LOG
from tests.resolve.fake_resolve import make_world
from tests.resolve.fake_ui import FakeBackend, accept_all

FAKE_ENGINE = os.path.join(os.path.dirname(__file__), "fake_engine.py")


@pytest.fixture
def config():
    return {
        "engine": [sys.executable, "-I", FAKE_ENGINE],
        "model": "fake-model",
        "naming_provider": "heuristic",
        "engine_args": ["--with-voices"],  # the fake engine writes voices.json for SPEAKER_00 and SPEAKER_01
    }


@pytest.fixture
def saves(tmp_path, monkeypatch):
    """The argv of every `voices save` the fake engine gets."""
    log = tmp_path / "voices.log"
    monkeypatch.setenv(VOICES_LOG, str(log))
    monkeypatch.delenv(VOICES_FAIL, raising=False)

    def read():
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]

    return read


def _names(argv):
    """The ``--name SPEAKER=NAME`` pairs of a ``voices save`` argv."""
    return dict(argv[i + 1].partition("=")[::2] for i, arg in enumerate(argv[:-1]) if arg == "--name")


def _run(config, answer, **kwargs):
    resolve = make_world()
    backend = FakeBackend(answer)
    outcome = app.main(resolve=resolve, backend=backend, config=config, sleep=lambda s: None, **kwargs)
    assert outcome.error is None and not outcome.cancelled, backend.errors
    return outcome, backend, resolve


def test_confirmed_names_are_saved_with_the_run_s_voices(config, saves):
    outcome, backend, resolve = _run(config, accept_all(names={"SPEAKER_01": "Jordan"}))
    (argv,) = saves()
    assert argv[:3] == ["voices", "save", "--from"]
    assert os.path.basename(argv[3]) == "out"  # the run's --out directory, holding voices.json
    # Maria was the engine's guess, left as it was: confirmed too
    assert _names(argv) == {"SPEAKER_00": "Maria", "SPEAKER_01": "Jordan"}
    assert outcome.voices_saved == ["Maria", "Jordan"] and outcome.warnings == []
    assert "Voices remembered for next time: Maria, Jordan." in backend.messages[0][1]
    assert any(line.startswith("Engine: ") and " voices save " in line for line in backend.progress_views[1].lines)
    assert [t.name for t in resolve.project.timeline.tracks["subtitle"]] == ["Maria", "Jordan"]


def test_a_speaker_left_unnamed_is_not_saved(config, saves):
    outcome, _backend, _resolve = _run(config, accept_all())  # SPEAKER_01 keeps its id: no name
    (argv,) = saves()
    assert _names(argv) == {"SPEAKER_00": "Maria"}
    assert outcome.voices_saved == ["Maria"]


def test_nothing_confirmed_starts_no_save(config, saves):
    _run(config, accept_all(names={"SPEAKER_00": "SPEAKER_00"}))  # placeholder ids are not names
    assert saves() == []


def test_a_merged_speaker_is_saved_as_the_one_it_was_merged_into(config, saves):
    def answer(request):
        request.editor.merge("SPEAKER_01", "SPEAKER_00")
        request.speakers[:] = [r for r in request.speakers if r.id == "SPEAKER_00"]
        return accept_all(names={"SPEAKER_00": "Maria Lopez"})(request)

    _run(config, answer)
    (argv,) = saves()
    assert _names(argv) == {"SPEAKER_00": "Maria Lopez", "SPEAKER_01": "Maria Lopez"}


def test_a_speaker_whose_lines_were_split_between_people_is_not_saved(config, saves):
    def answer(request):
        first_maria = next(c.index for c in request.editor.cues("SPEAKER_00"))
        request.editor.reassign([first_maria], "SPEAKER_01")  # half of SPEAKER_00's lines were Jordan's
        return accept_all(names={"SPEAKER_01": "Jordan"})(request)

    _run(config, answer)
    (argv,) = saves()
    assert _names(argv) == {"SPEAKER_01": "Jordan"}


def test_no_voices_from_the_engine_starts_no_save(config, saves):
    config["engine_args"] = []
    outcome, _backend, _resolve = _run(config, accept_all(names={"SPEAKER_01": "Jordan"}))
    assert saves() == [] and outcome.voices_saved == [] and outcome.warnings == []


def test_the_voice_library_can_be_turned_off(config, saves):
    config[config_mod.VOICE_LIBRARY] = False
    _outcome, backend, _resolve = _run(config, accept_all(names={"SPEAKER_01": "Jordan"}))
    assert saves() == []
    engine_line = next(line for line in backend.progress_views[0].lines if line.startswith("Engine: "))
    assert "--no-voice-library" in engine_line


@pytest.mark.parametrize("failure", ["error", "crash"])
def test_a_failed_save_is_only_a_warning(config, saves, monkeypatch, failure):
    monkeypatch.setenv(VOICES_FAIL, failure)
    outcome, backend, resolve = _run(config, accept_all(names={"SPEAKER_01": "Jordan"}))
    assert len(saves()) == 1
    assert outcome.voices_saved == []
    (warning,) = outcome.warnings
    assert warning.startswith("voices not remembered: ")
    assert ("voice library is broken" if failure == "error" else "code 3") in warning
    assert [t.name for t in resolve.project.timeline.tracks["subtitle"]] == ["Maria", "Jordan"]  # tracks still made
    assert "Warning: voices not remembered" in backend.messages[0][1]


def test_a_save_that_hangs_is_stopped(config, saves, monkeypatch):
    monkeypatch.setenv(VOICES_FAIL, "hang")
    monkeypatch.setattr(app, "VOICE_SAVE_TIMEOUT", 0.5)
    outcome, _backend, resolve = _run(config, accept_all(names={"SPEAKER_01": "Jordan"}))
    assert outcome.warnings == ["voices not remembered: the engine took longer than 0.5 s"]
    assert len(resolve.project.timeline.tracks["subtitle"]) == 2


def test_an_unusable_engine_command_is_only_a_warning(config, saves, tmp_path):
    outcome = app.Outcome()
    voices = tmp_path / "voices.json"
    voices.write_text(json.dumps({"model": "m", "speakers": {"SPEAKER_00": {"centroid": [1.0]}}}))
    editor = app.speaker_edits.SpeakerEditor(json.loads(json.dumps(TRANSCRIPT)))
    app.remember_voices({"engine": ["/no/such/python"]}, editor, str(voices), outcome)
    assert outcome.warnings and outcome.warnings[0].startswith("voices not remembered: the engine Python")
    assert saves() == []


# --- voice_library on its own


def _transcript(*speakers):
    data = json.loads(json.dumps(TRANSCRIPT))
    data["speakers"] = [{"id": sid, "name": name} for sid, name in speakers]
    return data


def test_confirmed_names_follow_the_cues():
    original = _transcript(("SPEAKER_00", None), ("SPEAKER_01", None))
    reviewed = json.loads(json.dumps(original))
    reviewed["speakers"] = [{"id": "SPEAKER_00", "name": " Maria  Lopez "}, {"id": "SPEAKER_01", "name": None}]
    assert voice_library.confirmed_names(original, reviewed, ["SPEAKER_00", "SPEAKER_01", "SPEAKER_07"]) == {
        "SPEAKER_00": "Maria Lopez"
    }
    # SPEAKER_01's longer cue (1.5 s of 3.1 s) went to SPEAKER_00: under MIN_SHARE either way
    reviewed["cues"][1]["speaker"] = "SPEAKER_00"
    reviewed["speakers"][1]["name"] = "Jordan"
    assert voice_library.confirmed_names(original, reviewed, ["SPEAKER_01"]) == {}
    # all of them moved: SPEAKER_01's voice is Maria's
    reviewed["cues"][3]["speaker"] = "SPEAKER_00"
    assert voice_library.confirmed_names(original, reviewed, ["SPEAKER_01"]) == {"SPEAKER_01": "Maria Lopez"}


def test_real_names():
    assert voice_library.is_real_name("Anthony", "SPEAKER_00")
    for placeholder in ("", "  ", "SPEAKER_00", "speaker 3", "PINNED_01", "Speaker7"):
        assert not voice_library.is_real_name(placeholder, "SPEAKER_00")
    assert not voice_library.is_real_name("S1", "S1")


def test_voice_speakers_and_path(tmp_path):
    path = tmp_path / "voices.json"
    assert voice_library.voice_speakers(str(path)) == []
    path.write_text("{oops")
    assert voice_library.voice_speakers(str(path)) == []
    path.write_text(json.dumps({"speakers": {"SPEAKER_00": {}, "SPEAKER_02": {}}}))
    assert voice_library.voice_speakers(str(path)) == ["SPEAKER_00", "SPEAKER_02"]
    assert voice_library.voices_path("out", {"voices": str(path)}) == str(path)
    assert voice_library.voices_path("out", {"voices": None}) == os.path.join("out", "voices.json")


def test_voices_command(config):
    argv = config_mod.voices_command(config, "/w/out", {"SPEAKER_00": "Maria", "SPEAKER_01": "A=B"})
    names = ["--name", "SPEAKER_00=Maria", "--name", "SPEAKER_01=A=B"]
    assert argv == [*config["engine"], "voices", "save", "--from", "/w/out", *names]
    assert "--no-voice-library" not in config_mod.engine_command(config, "a.wav", "out")
    with pytest.raises(config_mod.ConfigError):
        config_mod.voices_command({}, "out", {"SPEAKER_00": "Maria"})
