"""Pause / resume: the Pause button, the flow holding between steps, and the engine process being suspended."""

import os
import signal
import subprocess
import sys
import types

import pytest

from better_captions_plugin import app, engine_runner
from better_captions_plugin.engine_runner import EngineCancelled, run_engine
from better_captions_plugin.ui.uimanager_ui import UIManagerBackend
from tests.resolve.fake_resolve import make_world
from tests.resolve.fake_ui import FakeBackend, FakeProgress, accept_all
from tests.resolve.test_ui import FakeBmd, FakeFusion

FAKE_ENGINE = os.path.join(os.path.dirname(__file__), "fake_engine.py")
HAS_PROC = os.path.isfile("/proc/self/stat")
DIARIZE = app.STEP_ENGINE + engine_runner.STAGES.index("diarize")


@pytest.fixture
def config():
    return {
        "engine": [sys.executable, "-I", FAKE_ENGINE],
        "engine_args": ["--slow"],
        "model": "fake-model",
        "naming_provider": "heuristic",
    }


@pytest.fixture
def wav(tmp_path):
    path = tmp_path / "audio.wav"
    path.write_bytes(b"RIFF")
    return path


def _argv(audio, out, *extra):
    return [sys.executable, "-I", FAKE_ENGINE, "run", str(audio), "--out", str(out), *extra]


def _proc_state(pid):
    with open("/proc/%d/stat" % pid) as handle:
        return handle.read().rsplit(")", 1)[1].split()[0]


class Recorder:
    """A popen that keeps the processes it started."""

    def __init__(self):
        self.processes = []

    def __call__(self, argv, **kwargs):
        process = subprocess.Popen(argv, **kwargs)
        self.processes.append(process)
        return process


# --- the Pause button


def test_uimanager_pause_button_toggles_and_cancel_clears_it():
    bmd = FakeBmd()
    view = UIManagerBackend(FakeFusion(), bmd).progress("t")
    window = bmd.dispatcher.windows[0]
    items = window.GetItems()
    view.set_status("Transcribing…")
    assert view.paused is False and items["Pause"].Text == "Pause"
    window.On.Pause.Clicked()
    assert view.paused is True and items["Pause"].Text == "Resume"
    assert items["Status"].Text == "Paused – Transcribing…"
    view.set_status("Detecting speakers…")  # a new status keeps saying it is paused
    assert items["Status"].Text == "Paused – Detecting speakers…"
    window.On.Pause.Clicked()
    assert view.paused is False and items["Pause"].Text == "Pause"
    assert items["Status"].Text == "Detecting speakers…"
    window.On.Pause.Clicked()
    window.On.Cancel.Clicked()  # cancelling while paused ends the pause so the engine can be stopped
    assert view.cancelled is True and view.paused is False
    window.On.Pause.Clicked()  # and Pause does nothing any more
    assert view.paused is False


def test_tk_pause_button_is_wired():
    from tests.resolve.conftest import skip_unless_gui_tests

    skip_unless_gui_tests()
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip("no display for tkinter: %s" % exc)
    root.withdraw()
    try:
        from better_captions_plugin.ui.tk_ui import TkProgress

        view = TkProgress(root, "t")
        view.set_status("Transcribing…")
        view._toggle_pause()
        assert view.paused and view._status.get() == "Paused – Transcribing…"
        view._toggle_pause()
        assert not view.paused and view._status.get() == "Transcribing…"
        view.close()
    finally:
        root.destroy()


# --- the flow holds at step boundaries


def test_hold_while_paused_returns_at_once_when_not_paused():
    view = FakeProgress("t")
    app.hold_while_paused(view, sleep=lambda s: pytest.fail("must not wait"))
    assert view.lines == []


def test_flow_holds_before_the_next_step_until_resumed(config):
    config["engine_args"] = []
    backend = FakeBackend(
        accept_all(),
        pause_when=lambda view: view.step_index == app.STEP_RENDER,
        resume_when=lambda view: view.paused_pumps >= 4,
    )
    outcome = app.main(resolve=make_world(), backend=backend, config=config, sleep=lambda s: None)
    assert outcome.error is None and not outcome.cancelled, backend.errors
    view = backend.progress_views[0]
    assert view.resumes == 1 and view.paused_pumps >= 4
    # It waited after rendering, before the engine step and before the engine was started.
    lines = view.lines
    held = lines.index("Paused. Press Resume to continue.")
    assert held < lines.index("Resumed.") < next(i for i, line in enumerate(lines) if line.startswith("Engine: "))
    assert (app.STEP_ENGINE, False) in view.step_log  # moved on only once running again
    assert outcome.created  # and the run finished normally


def test_flow_holds_the_engine_and_every_step_while_paused(config):
    recorder = Recorder()
    backend = FakeBackend(
        accept_all(),
        pause_when=lambda view: view.step_index == DIARIZE,
        resume_when=lambda view: view.paused_pumps >= 8,
    )
    outcome = app.main(
        resolve=make_world(), backend=backend, config=config, sleep=lambda s: None, popen=recorder
    )
    assert outcome.error is None and not outcome.cancelled, backend.errors
    view = backend.progress_views[0]
    assert view.resumes == 1
    assert [paused for _index, paused in view.step_log] == [False] * len(view.step_log)  # no step moved while paused
    assert view.step_log[-1][0] == app.STEP_REVIEW and outcome.created
    assert recorder.processes[0].returncode == 0  # the suspended engine was continued and finished


def test_cancel_while_paused_between_steps(config):
    backend = FakeBackend(
        accept_all(),
        pause_when=lambda view: view.step_index == app.STEP_RENDER,
        cancel_when=lambda view: view.paused_pumps >= 3,
    )
    outcome = app.main(resolve=make_world(), backend=backend, config=config, sleep=lambda s: None)
    assert outcome.cancelled and outcome.error is None and outcome.created == []
    view = backend.progress_views[0]
    assert view.closed and not any(line.startswith("Engine: ") for line in view.lines)
    assert backend.requests == [] and backend.errors == []


def test_cancel_while_the_engine_is_paused_stops_it(config):
    recorder = Recorder()
    backend = FakeBackend(
        accept_all(),
        pause_when=lambda view: view.step_index == DIARIZE,
        cancel_when=lambda view: view.paused_pumps >= 4,
    )
    outcome = app.main(
        resolve=make_world(), backend=backend, config=config, sleep=lambda s: None, popen=recorder
    )
    assert outcome.cancelled and outcome.error is None and outcome.created == []
    assert backend.requests == [] and backend.errors == []
    process = recorder.processes[0]
    assert process.poll() is not None and process.returncode != 0  # not left stopped, not run to the end


# --- engine_runner: the process itself


@pytest.mark.skipif(not HAS_PROC, reason="reads the process state from /proc")
def test_run_engine_suspends_the_process_and_holds_events_until_resumed(wav, tmp_path):
    recorder = Recorder()
    events, states, counts = [], [], []
    ticks = [0]

    def started():
        return any(e["event"] == "progress" for e in events)

    def pump():
        if started():
            ticks[0] += 1
            if 3 <= ticks[0] <= 10:  # well inside the paused stretch
                states.append(_proc_state(recorder.processes[0].pid))
                counts.append(len(events))

    done = run_engine(
        _argv(wav, tmp_path / "out", "--slow"),
        on_event=events.append,
        pump=pump,
        should_pause=lambda: started() and ticks[0] <= 10,
        popen=recorder,
        poll_interval=0.02,
    )
    assert done["event"] == "done"
    assert states and set(states) == {"T"}  # stopped (SIGSTOP) while paused
    assert len(set(counts)) == 1  # nothing was handed out while paused
    assert len(events) > counts[0]  # and the rest came after resuming


@pytest.mark.skipif(not HAS_PROC, reason="reads the process state from /proc")
def test_run_engine_cancel_of_a_paused_engine_resumes_then_terminates(wav, tmp_path):
    recorder = Recorder()
    events = []
    ticks = [0]

    def pump():
        if any(e["event"] == "progress" for e in events):
            ticks[0] += 1

    with pytest.raises(EngineCancelled):
        run_engine(
            _argv(wav, tmp_path / "out", "--slow"),
            on_event=events.append,
            pump=pump,
            should_pause=lambda: ticks[0] > 0,
            should_cancel=lambda: ticks[0] >= 6,
            popen=recorder,
            poll_interval=0.02,
        )
    process = recorder.processes[0]
    assert process.poll() is not None  # a stopped process would ignore SIGTERM and be killed only after 5 s
    assert not (tmp_path / "out" / "transcript.json").exists()


def test_run_engine_without_should_pause_never_signals(wav, tmp_path, monkeypatch):
    monkeypatch.setattr(engine_runner, "_set_suspended", lambda process, suspend: pytest.fail("signalled"))
    assert run_engine(_argv(wav, tmp_path / "out"))["event"] == "done"


def test_run_engine_that_cannot_be_paused_keeps_running_and_says_so(wav, tmp_path, monkeypatch):
    def refuse(process, suspend):
        raise OSError("no permission")

    monkeypatch.setattr(engine_runner, "_set_suspended", refuse)
    events = []
    done = run_engine(_argv(wav, tmp_path / "out"), on_event=events.append, should_pause=lambda: True)
    assert done["event"] == "done"
    logs = [e["line"] for e in events if e["event"] == "log" and "could not pause" in e["line"]]
    assert logs == ["could not pause the engine: no permission"]  # once, not on every poll


def test_posix_suspend_sends_sigstop_and_sigcont(monkeypatch):
    sent = []
    monkeypatch.setattr(engine_runner, "sys", types.SimpleNamespace(platform="linux"))
    monkeypatch.setattr(engine_runner, "os", types.SimpleNamespace(kill=lambda pid, sig: sent.append((pid, sig))))
    process = types.SimpleNamespace(pid=4242)
    engine_runner._set_suspended(process, True)
    engine_runner._set_suspended(process, False)
    assert sent == [(4242, signal.SIGSTOP), (4242, signal.SIGCONT)]


class FakeWindowsApi:
    """ctypes.windll with kernel32 / ntdll recording their calls."""

    def __init__(self, handle=77, status=0):
        self.calls = []
        self.handle = handle
        self.status = status
        api = self

        def open_process(access, inherit, pid):
            api.calls.append(("OpenProcess", access, inherit, pid))
            return api.handle

        def close_handle(handle):
            api.calls.append(("CloseHandle", handle))

        def nt_suspend(handle):
            api.calls.append(("NtSuspendProcess", handle))
            return api.status

        def nt_resume(handle):
            api.calls.append(("NtResumeProcess", handle))
            return api.status

        kernel32 = types.SimpleNamespace(OpenProcess=open_process, CloseHandle=close_handle)
        ntdll = types.SimpleNamespace(NtSuspendProcess=nt_suspend, NtResumeProcess=nt_resume)
        self.ctypes = types.SimpleNamespace(
            windll=types.SimpleNamespace(kernel32=kernel32, ntdll=ntdll), c_void_p=lambda value: value
        )


def _windows(monkeypatch, api):
    monkeypatch.setattr(engine_runner, "sys", types.SimpleNamespace(platform="win32"))
    monkeypatch.setattr(engine_runner, "ctypes", api.ctypes)


def test_windows_suspend_and_resume_use_ntsuspendprocess(monkeypatch):
    api = FakeWindowsApi()
    _windows(monkeypatch, api)
    process = types.SimpleNamespace(pid=999)
    engine_runner._set_suspended(process, True)
    engine_runner._set_suspended(process, False)
    access = engine_runner.PROCESS_SUSPEND_RESUME
    assert api.calls == [
        ("OpenProcess", access, False, 999),
        ("NtSuspendProcess", 77),
        ("CloseHandle", 77),
        ("OpenProcess", access, False, 999),
        ("NtResumeProcess", 77),
        ("CloseHandle", 77),
    ]


def test_windows_suspend_reports_failures_and_still_closes_the_handle(monkeypatch):
    api = FakeWindowsApi(status=0xC0000022)
    _windows(monkeypatch, api)
    with pytest.raises(OSError, match="NtSuspendProcess failed"):
        engine_runner._set_suspended(types.SimpleNamespace(pid=1), True)
    assert api.calls[-1] == ("CloseHandle", 77)
    api = FakeWindowsApi(handle=0)
    _windows(monkeypatch, api)
    with pytest.raises(OSError, match="OpenProcess failed"):
        engine_runner._set_suspended(types.SimpleNamespace(pid=1), False)
