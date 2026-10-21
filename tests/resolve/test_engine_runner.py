"""The engine runner against the fake engine run as a real subprocess."""

import json
import os
import sys

import pytest

from better_captions_plugin import engine_runner
from better_captions_plugin.engine_runner import EngineCancelled, EngineError, parse_event, run_engine

FAKE_ENGINE = os.path.join(os.path.dirname(__file__), "fake_engine.py")


def _argv(audio, out, *extra):
    return [sys.executable, "-I", FAKE_ENGINE, "run", str(audio), "--out", str(out), *extra]


@pytest.fixture
def wav(tmp_path):
    path = tmp_path / "audio.wav"
    path.write_bytes(b"RIFF")
    return path


def test_parse_event_forwards_non_json_as_log():
    assert parse_event('{"event": "progress", "stage": "transcribe", "pct": 5}\n') == {"event": "progress", "stage": "transcribe", "pct": 5}
    assert parse_event("Downloading model...\n") == {"event": "log", "line": "Downloading model..."}
    assert parse_event('{"not": "an event"}') == {"event": "log", "line": '{"not": "an event"}'}
    assert parse_event("{broken json") == {"event": "log", "line": "{broken json"}


def test_stage_labels():
    assert engine_runner.stage_label("diarize") == "Detecting speakers"
    assert engine_runner.stage_label("new_stage") == "New stage"


def test_run_engine_streams_events_and_returns_done(wav, tmp_path):
    out = tmp_path / "out"
    events = []
    pumps = []
    done = run_engine(_argv(wav, out), on_event=events.append, pump=lambda: pumps.append(1))
    assert done["event"] == "done"
    assert set(done["srt"]) == {"SPEAKER_00", "SPEAKER_01"}
    assert os.path.isfile(done["transcript"])
    kinds = [e["event"] for e in events]
    assert kinds.count("progress") == 6 and kinds[-1] == "done"
    assert {"event": "log", "line": "not json: model loaded"} in events
    assert {"event": "log", "line": "warning: downloading model (fake)"} in events
    pcts = [e["pct"] for e in events if e["event"] == "progress"]
    assert pcts == sorted(pcts) and pcts[-1] == 100.0
    assert pumps  # the UI got a chance to repaint while waiting
    transcript = json.load(open(done["transcript"], encoding="utf-8"))
    assert transcript["meta"]["audio"] == str(wav)


def test_run_engine_reports_engine_error_event(wav, tmp_path):
    with pytest.raises(EngineError, match="no speech found"):
        run_engine(_argv(wav, tmp_path / "out", "--fail"))


def test_run_engine_reports_crash_with_stderr_tail(wav, tmp_path):
    with pytest.raises(EngineError) as info:
        run_engine(_argv(wav, tmp_path / "out", "--crash"))
    assert "exited with code 3" in str(info.value)
    assert "RuntimeError: engine crashed" in str(info.value)


def test_run_engine_missing_audio_is_an_engine_error(tmp_path):
    with pytest.raises(EngineError, match="FileNotFoundError"):
        run_engine(_argv(tmp_path / "nope.wav", tmp_path / "out"))


def test_run_engine_unstartable_command(tmp_path):
    with pytest.raises(EngineError, match="could not start the engine"):
        run_engine([str(tmp_path / "no-such-python"), "run"])


def test_run_engine_cancel_terminates_process(wav, tmp_path):
    seen = []

    def should_cancel():
        return any(e["event"] == "progress" for e in seen)

    with pytest.raises(EngineCancelled):
        run_engine(_argv(wav, tmp_path / "out", "--slow"), on_event=seen.append, should_cancel=should_cancel, poll_interval=0.02)
    assert not (tmp_path / "out" / "transcript.json").exists()
