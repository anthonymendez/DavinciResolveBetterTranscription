"""Debug log: everything a developer needs to diagnose a run remotely.

On when config.json has ``"debug": true`` (``install-debug.ps1`` / ``install-debug.sh``) or
``BETTER_CAPTIONS_DEBUG=1`` is set. Each run then writes
``<data>/logs/better-captions-YYYYMMDD-HHMMSS.log`` (the newest ``KEEP`` are kept) with:

- the plugin, Python, OS and Resolve versions, config.json (secrets redacted), the UI backend
- every Resolve API call, through ``Tracer``: a transparent proxy around ``resolve`` that also
  wraps every Resolve object it returns, logging arguments, return value, duration and exceptions
- a render diagnostics snapshot (formats, codecs, presets) taken before rendering
- the engine command, environment changes, every stdout/stderr line and the exit code
- tracebacks for errors and the final summary

When debug is off nothing here runs: ``start`` returns None, nothing is wrapped and every
module-level helper is a no-op.
"""

from __future__ import annotations

import os
import platform
import sys
import threading
import time
import traceback

LOGS_DIR = "logs"
PREFIX = "better-captions-"
KEEP = 20
REPR_LIMIT = 2000
SECRET_WORDS = ("key", "token", "secret")
REDACTED = "***redacted***"

_PLAIN = (str, bytes, int, float, complex, bool, type(None))
_current = None  # the DebugLog of the run in progress


def enabled(config, environ=None) -> bool:
    env = os.environ if environ is None else environ
    if str(env.get("BETTER_CAPTIONS_DEBUG", "")).strip().lower() in ("1", "true", "yes", "on"):
        return True
    return isinstance(config, dict) and config.get("debug") is True


def current():
    return _current


def logs_dir(config, environ=None) -> str:
    """``<data>/logs``, next to the config.json the run loaded (else config.py's data dir)."""
    from better_captions_plugin import config as config_mod

    path = config.get("_path") if isinstance(config, dict) else None
    base = os.path.dirname(str(path)) if path else config_mod.data_dir(environ)
    return os.path.join(base, LOGS_DIR)


def start(config, environ=None):
    """Open this run's log when debug is on; returns the DebugLog, or None (never raises)."""
    global _current
    if _current is not None:
        return _current
    if not enabled(config, environ):
        return None
    try:
        directory = logs_dir(config, environ)
        os.makedirs(directory, exist_ok=True)
        prune(directory, KEEP - 1)
        log = DebugLog(_new_log_path(directory))
    except Exception as exc:  # a debug log must never stop a run
        print("Better Captions: could not start the debug log: %s" % exc)
        return None
    _current = log
    log.header(config)
    return log


def finish(outcome=None):
    """Log the final outcome and close the log."""
    global _current
    log, _current = _current, None
    if log is None:
        return
    if outcome is not None:
        log.section("Outcome")
        log.write("error: %s" % outcome.error)
        log.write("cancelled: %s" % outcome.cancelled)
        log.write("work dir: %s (kept: %s)" % (outcome.work_dir, outcome.kept_work_dir))
        log.write("summary:\n%s" % outcome.summary())
    log.write("log closed")
    log.close()


def with_path(text: str) -> str:
    """``text`` + "Debug log: <path>" while a log is open; unchanged otherwise."""
    if _current is None:
        return text
    return "%s\n\nDebug log: %s" % (text, _current.path)


def note(text: str):
    if _current is not None:
        _current.write(text)


def exception(context: str):
    """Log the exception being handled, with its traceback."""
    if _current is not None:
        _current.write("%s\n%s" % (context, traceback.format_exc().rstrip()))


def prune(directory: str, keep: int):
    """Delete all but the newest ``keep`` logs (names sort by time)."""
    names = sorted(n for n in os.listdir(directory) if n.startswith(PREFIX) and n.endswith(".log"))
    for name in names[: max(0, len(names) - keep)]:
        try:
            os.remove(os.path.join(directory, name))
        except OSError:
            pass


def _new_log_path(directory: str) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = os.path.join(directory, "%s%s.log" % (PREFIX, stamp))
    n = 2
    while os.path.exists(path):  # two runs within a second
        path = os.path.join(directory, "%s%s-%d.log" % (PREFIX, stamp, n))
        n += 1
    return path


def truncate(text: str, limit: int = REPR_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return "%s... (%d more chars)" % (text[:limit], len(text) - limit)


def redact(value):
    """A copy of ``value`` with every dict entry whose key mentions key/token/secret replaced."""
    if isinstance(value, dict):
        return {
            k: (REDACTED if any(w in str(k).lower() for w in SECRET_WORDS) else redact(v)) for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    return value


class DebugLog:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._handle = open(path, "a", encoding="utf-8", errors="replace")  # noqa: SIM115 - open for the whole run
        self._tracer = None

    def write(self, text: str):
        stamp = time.strftime("%H:%M:%S") + ".%03d" % (int(time.time() * 1000) % 1000)
        with self._lock:
            if self._handle is None:
                return
            try:
                self._handle.write("%s %s\n" % (stamp, text))
                self._handle.flush()  # a crash or a killed Resolve still leaves the log
            except Exception:
                pass

    def section(self, title: str):
        self.write("===== %s =====" % title)

    def close(self):
        with self._lock:
            if self._handle is not None:
                self._handle.close()
                self._handle = None

    def header(self, config):
        from better_captions_plugin import __version__

        self.section("Better Captions debug log")
        self.write("plugin version: %s" % __version__)
        self.write("python: %s" % sys.version.replace("\n", " "))
        self.write("python executable: %s" % sys.executable)
        self.write("os: %s (%s, %s)" % (platform.platform(), sys.platform, platform.machine()))
        self.write("log file: %s" % self.path)
        self.write("config: %s" % truncate(repr(redact(config)), 20000))

    def wrap(self, resolve):
        """``resolve`` behind a tracing proxy (None stays None)."""
        if self._tracer is None:
            self._tracer = Tracer(self.write)
        return self._tracer.wrap(resolve, "resolve")


# ---------------------------------------------------------------------------
# Tracing proxy


class ResolveProxy:
    """Forwards everything to a Resolve object; method calls go through the Tracer."""

    __slots__ = ("_bc_label", "_bc_target", "_bc_tracer")

    def __init__(self, target, tracer, label):
        object.__setattr__(self, "_bc_target", target)
        object.__setattr__(self, "_bc_tracer", tracer)
        object.__setattr__(self, "_bc_label", label)

    def __getattr__(self, name):
        value = getattr(self._bc_target, name)
        if callable(value):
            return self._bc_tracer.method(self, name, value)
        return self._bc_tracer.wrap(value, "%s.%s" % (self._bc_label, name))

    def __setattr__(self, name, value):
        setattr(self._bc_target, name, unwrap(value))

    def __repr__(self):
        return repr(self._bc_target)

    def __str__(self):
        return str(self._bc_target)

    def __eq__(self, other):
        return self._bc_target == unwrap(other)

    def __ne__(self, other):
        return self._bc_target != unwrap(other)

    def __hash__(self):
        return hash(self._bc_target)

    def __bool__(self):
        return bool(self._bc_target)


def unwrap(value):
    """The real objects behind proxies, also inside lists, tuples and dicts (for calls into Resolve)."""
    if isinstance(value, ResolveProxy):
        return value._bc_target
    if isinstance(value, list):
        return [unwrap(v) for v in value]
    if isinstance(value, tuple):
        return tuple(unwrap(v) for v in value)
    if isinstance(value, dict):
        return {unwrap(k): unwrap(v) for k, v in value.items()}
    return value


class Tracer:
    """Wraps Resolve objects in ResolveProxy and logs each call through ``write(line)``.

    The same object always gets the same proxy, so identity checks still hold; plain values
    (None, numbers, strings) are returned as they are, lists and dicts as copies whose Resolve
    objects are wrapped.
    """

    def __init__(self, write, limit: int = REPR_LIMIT):
        self.write = write
        self.limit = limit
        self._proxies = {}  # id(target) -> (target, proxy); the target is kept alive so ids stay unique
        self._count = 0

    def wrap(self, value, label="object"):
        if isinstance(value, (*_PLAIN, ResolveProxy)):
            return value
        if isinstance(value, list):
            return [self.wrap(v, "%s[%d]" % (label, i)) for i, v in enumerate(value)]
        if isinstance(value, tuple):
            return tuple(self.wrap(v, "%s[%d]" % (label, i)) for i, v in enumerate(value))
        if isinstance(value, dict):
            return {k: self.wrap(v, "%s[%r]" % (label, k)) for k, v in value.items()}
        known = self._proxies.get(id(value))
        if known is not None and known[0] is value:
            return known[1]
        self._count += 1
        proxy = ResolveProxy(value, self, "%s#%d" % (label, self._count))
        self._proxies[id(value)] = (value, proxy)
        return proxy

    def method(self, owner: ResolveProxy, name: str, fn):
        tracer = self

        def traced(*args, **kwargs):
            call = "%s.%s(%s)" % (owner._bc_label, name, tracer._arguments(args, kwargs))
            began = time.perf_counter()
            try:
                result = fn(*unwrap(args), **unwrap(kwargs))
            except Exception as exc:
                tracer.write(
                    "API %s raised %s: %s (%.1f ms)\n%s"
                    % (call, type(exc).__name__, exc, _ms(began), traceback.format_exc().rstrip())
                )
                raise
            label = name[3:] if name.startswith("Get") and len(name) > 3 else name
            wrapped = tracer.wrap(result, label)
            tracer.write("API %s -> %s (%.1f ms)" % (call, tracer.describe(wrapped), _ms(began)))
            return wrapped

        return traced

    def describe(self, value) -> str:
        return truncate(self._describe(value), self.limit)

    def _describe(self, value) -> str:
        if isinstance(value, ResolveProxy):
            return "<%s>" % value._bc_label
        if isinstance(value, list):
            return "[%s]" % ", ".join(self._describe(v) for v in value)
        if isinstance(value, tuple):
            return "(%s)" % ", ".join(self._describe(v) for v in value)
        if isinstance(value, dict):
            return "{%s}" % ", ".join("%r: %s" % (k, self._describe(v)) for k, v in value.items())
        try:
            return repr(value)
        except Exception as exc:
            return "<unrepresentable %s: %s>" % (type(value).__name__, exc)

    def _arguments(self, args, kwargs) -> str:
        parts = [self.describe(a) for a in args]
        parts.extend("%s=%s" % (k, self.describe(v)) for k, v in kwargs.items())
        return ", ".join(parts)


def _ms(began: float) -> float:
    return (time.perf_counter() - began) * 1000.0


# ---------------------------------------------------------------------------
# Run details


def render_diagnostics(project):
    """Log what Resolve offers for rendering; each call may fail without affecting the run."""
    log = _current
    if log is None or project is None:
        return
    project = unwrap(project)
    log.section("Render diagnostics")

    def probe(label, fn):
        try:
            value = fn()
        except Exception as exc:
            log.write("%s raised %s: %s" % (label, type(exc).__name__, exc))
            return None
        log.write("%s -> %s" % (label, truncate(repr(value))))
        return value

    formats = probe("GetRenderFormats()", lambda: project.GetRenderFormats())
    if isinstance(formats, dict):
        for key, ext in formats.items():
            # Resolve's docs say GetRenderCodecs takes the format's extension; some versions want its name.
            probe("GetRenderCodecs(%r)" % (key,), lambda k=key: project.GetRenderCodecs(k))
            if ext != key:
                probe("GetRenderCodecs(%r)" % (ext,), lambda e=ext: project.GetRenderCodecs(e))
    for name in ("GetRenderPresetList", "GetCurrentRenderFormatAndCodec", "GetCurrentRenderMode", "GetRenderSettings"):
        try:
            fn = getattr(project, name)
        except Exception:
            log.write("%s() is not available" % name)
            continue
        probe("%s()" % name, fn)
    log.section("End of render diagnostics")


def engine_start(argv, env, base=None):
    """Log the engine command and how its environment differs from the plugin's."""
    log = _current
    if log is None:
        return
    base = os.environ if base is None else base
    log.section("Engine")
    log.write("command: %r" % (list(argv),))
    env = env or {}
    for key in sorted(set(base) - set(env)):
        log.write("env removed: %s=%s" % (key, _env_value(key, base[key])))
    for key in sorted(env):
        if base.get(key) != env[key]:
            log.write("env set: %s=%s" % (key, _env_value(key, env[key])))


def engine_trace():
    """A ``trace(kind, text)`` for engine_runner.run_engine, or None when debug is off."""
    log = _current
    if log is None:
        return None

    def trace(kind, text):
        log.write("engine %s: %s" % (kind, text))

    return trace


def _env_value(key, value):
    return REDACTED if any(w in key.lower() for w in SECRET_WORDS) else truncate(str(value), 500)
