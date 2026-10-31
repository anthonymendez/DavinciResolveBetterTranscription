"""Run the engine as a subprocess and stream its JSON-line events.

Contract (docs/ARCHITECTURE.md): the engine writes one JSON object per stdout line,
``{"event": "progress", "stage": ..., "pct": ...}`` repeatedly, then ``{"event": "done", ...}``
or ``{"event": "error", "message": ...}``. Anything else on stdout is forwarded as a
``{"event": "log", "line": ...}`` event so model-download chatter still shows up.
"""

from __future__ import annotations

import ctypes
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time

STAGE_LABELS = {
    "transcribe": "Transcribing",
    "diarize": "Detecting speakers",
    "align": "Aligning words to speakers",
    "naming": "Naming speakers",
    "export": "Writing captions",
}
STAGES = tuple(STAGE_LABELS)  # in the order the engine runs them

PROCESS_SUSPEND_RESUME = 0x0800  # Windows access right for NtSuspendProcess / NtResumeProcess


class EngineError(Exception):
    pass


class EngineCancelled(EngineError):
    pass


def stage_label(stage) -> str:
    return STAGE_LABELS.get(str(stage), str(stage).replace("_", " ").capitalize())


def parse_event(line: str) -> dict:
    """A JSON object from one stdout line, or a ``log`` event for anything else."""
    text = line.strip()
    if text.startswith("{"):
        try:
            event = json.loads(text)
        except ValueError:
            event = None
        if isinstance(event, dict) and "event" in event:
            return event
    return {"event": "log", "line": line.rstrip("\r\n")}


def _reader(stream, sink: queue.Queue):
    try:
        for line in iter(stream.readline, ""):
            sink.put(("line", line))
    finally:
        try:
            stream.close()
        except Exception:
            pass
        sink.put(("eof", None))


def run_engine(
    argv,
    on_event=None,
    should_cancel=None,
    pump=None,
    env=None,
    poll_interval=0.05,
    popen=None,
    trace=None,
    should_pause=None,
) -> dict:
    """Run ``argv`` to completion; return the ``done`` event.

    ``on_event(event)`` is called for every parsed event; ``pump()`` is called between
    polls so a UI can repaint; ``should_cancel()`` returning True terminates the engine.
    While ``should_pause()`` is true the engine process is suspended and no events are handed
    out; it carries on when that turns false. Cancelling a paused engine resumes it, then stops it.
    ``trace(kind, text)``, if given, sees every raw stdout/stderr line and the exit code (debug log).
    Raises ``EngineError`` with the engine's message (or its stderr tail) on failure.
    """
    on_event = on_event or (lambda event: None)
    popen = popen or subprocess.Popen
    kwargs = dict(
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
        env=env,
        universal_newlines=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    if sys.platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        process = popen(list(argv), **kwargs)
    except OSError as exc:
        raise EngineError("could not start the engine (%s): %s" % (argv[0], exc)) from exc

    events: queue.Queue = queue.Queue()
    stderr_lines: list[str] = []
    threads = [
        threading.Thread(target=_reader, args=(process.stdout, events), daemon=True),
        threading.Thread(target=_stderr_reader, args=(process.stderr, stderr_lines, events), daemon=True),
    ]
    for thread in threads:
        thread.start()

    done = None
    error = None
    open_streams = 2
    last_pump = float("-inf")  # repaint as soon as the first event arrives
    suspended = False
    pause_failed = False
    while open_streams:
        if suspended:  # leave the events queued until the engine runs again
            kind = None
            time.sleep(poll_interval)
        else:
            try:
                kind, payload = events.get(timeout=poll_interval)
            except queue.Empty:
                kind = None
        # Keep the UI responsive even when the engine streams output non-stop.
        if kind is None or time.monotonic() - last_pump >= poll_interval:
            last_pump = time.monotonic()
            if pump:
                pump()
            if should_cancel and should_cancel():
                _terminate(process, resume=suspended)
                if trace:
                    trace("cancelled", "terminated by the user")
                raise EngineCancelled("cancelled")
            if should_pause and not pause_failed and bool(should_pause()) != suspended:
                try:
                    _set_suspended(process, not suspended)
                    suspended = not suspended
                    if trace:
                        trace("paused" if suspended else "resumed", "engine process")
                except Exception as exc:  # no way to freeze it here: let it run on
                    pause_failed = True
                    on_event({"event": "log", "line": "could not pause the engine: %s" % exc})
        if kind is None:
            continue
        if kind == "eof":
            open_streams -= 1
            continue
        if trace:
            trace("stdout" if kind == "line" else kind, payload.rstrip("\r\n"))
        if kind == "stderr":
            on_event({"event": "log", "line": payload})
            continue
        event = parse_event(payload)
        on_event(event)
        if event["event"] == "done":
            done = event
        elif event["event"] == "error":
            error = event
    returncode = process.wait()
    if trace:
        trace("exit", "code %s" % returncode)
    if error is not None:
        raise EngineError(str(error.get("message") or "engine failed"))
    if done is None or returncode != 0:
        tail = "\n".join(stderr_lines[-15:]).strip()
        raise EngineError(
            "engine exited with code %s without finishing%s" % (returncode, ("\n" + tail) if tail else "")
        )
    return done


def _stderr_reader(stream, store: list[str], sink: queue.Queue):
    try:
        for line in iter(stream.readline, ""):
            line = line.rstrip("\r\n")
            store.append(line)
            sink.put(("stderr", line))
    finally:
        try:
            stream.close()
        except Exception:
            pass
        sink.put(("eof", None))


def _set_suspended(process, suspend: bool):
    """Freeze (or thaw) the engine process: SIGSTOP/SIGCONT, or NtSuspendProcess/NtResumeProcess on Windows."""
    if sys.platform == "win32":
        _windows_suspend(process.pid, suspend)
    else:
        os.kill(process.pid, signal.SIGSTOP if suspend else signal.SIGCONT)


def _windows_suspend(pid: int, suspend: bool):
    kernel32 = ctypes.windll.kernel32
    ntdll = ctypes.windll.ntdll
    kernel32.OpenProcess.restype = ctypes.c_void_p
    handle = kernel32.OpenProcess(PROCESS_SUSPEND_RESUME, False, pid)
    if not handle:
        raise OSError("OpenProcess failed for pid %s" % pid)
    try:
        status = (ntdll.NtSuspendProcess if suspend else ntdll.NtResumeProcess)(ctypes.c_void_p(handle))
    finally:
        kernel32.CloseHandle(ctypes.c_void_p(handle))
    if status != 0:
        raise OSError("%s failed with status 0x%08X" % ("NtSuspendProcess" if suspend else "NtResumeProcess", status))


def _terminate(process, resume=False):
    if resume:  # a stopped process only acts on SIGTERM once it is running again
        try:
            _set_suspended(process, False)
        except Exception:
            pass
    try:
        process.terminate()
        for _ in range(50):
            if process.poll() is not None:
                return
            time.sleep(0.1)
        process.kill()
    except Exception:
        pass
