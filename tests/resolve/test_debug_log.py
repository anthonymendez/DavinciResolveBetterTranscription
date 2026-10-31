"""Debug log: the tracing proxy, redaction, log files, and a whole run with debug on against the fakes."""

import os
import sys

import pytest

from better_captions_plugin import app, debug_log
from tests.resolve.fake_resolve import make_world
from tests.resolve.fake_ui import FakeBackend, accept_all

FAKE_ENGINE = os.path.join(os.path.dirname(__file__), "fake_engine.py")


class Item:
    def __init__(self, name):
        self.name = name
        self.flag = "plain attribute"

    def GetName(self):
        return self.name

    def __repr__(self):
        return "Item(%r)" % self.name


class Folder:
    def __init__(self):
        self.items = [Item("a"), Item("b")]
        self.received = None

    def GetClipList(self):
        return self.items

    def GetClipMap(self):
        return {1: self.items[0], "count": 2}

    def GetFirst(self):
        return self.items[0]

    def GetNothing(self):
        return None

    def GetNumber(self):
        return 42

    def GetNames(self):
        return ["a", "b"]

    def Append(self, infos):
        self.received = infos
        return all(isinstance(info["item"], Item) for info in infos)

    def GetLong(self):
        return "x" * 5000

    def Fail(self, reason):
        raise RuntimeError("bad: %s" % reason)


@pytest.fixture
def traced():
    lines = []
    folder = Folder()
    return debug_log.Tracer(lines.append).wrap(folder, "folder"), folder, lines


def test_proxy_wraps_nested_objects_and_keeps_identity(traced):
    proxy, folder, lines = traced
    items = proxy.GetClipList()
    assert isinstance(items, list) and all(isinstance(i, debug_log.ResolveProxy) for i in items)
    assert items[0].GetName() == "a"
    # The same Resolve object always comes back as the same proxy.
    assert proxy.GetFirst() is items[0]
    assert proxy.GetClipMap()[1] is items[0] and proxy.GetClipMap()["count"] == 2
    assert items[0] == folder.items[0] and hash(items[0]) == hash(folder.items[0])
    assert repr(items[0]) == "Item('a')" and str(items[0]) == "Item('a')"
    assert items[0].flag == "plain attribute"
    assert any(line.startswith("API folder#1.GetClipList() -> [<ClipList[0]#2>, <ClipList[1]#3>]") for line in lines)
    assert any("API ClipList[0]#2.GetName() -> 'a'" in line for line in lines)


def test_plain_values_pass_through_unchanged(traced):
    proxy, _folder, _lines = traced
    assert proxy.GetNothing() is None
    assert proxy.GetNumber() == 42 and type(proxy.GetNumber()) is int
    assert proxy.GetNames() == ["a", "b"]
    assert debug_log.Tracer(print).wrap(None) is None
    assert debug_log.Tracer(print).wrap("text") == "text"


def test_arguments_are_unwrapped_before_reaching_resolve(traced):
    proxy, folder, lines = traced
    item = proxy.GetFirst()
    assert proxy.Append([{"item": item, "frame": 3}]) is True
    assert folder.received[0]["item"] is folder.items[0]
    assert any("folder#1.Append([{'item': <First#" in line for line in lines)


def test_exceptions_are_logged_and_reraised(traced):
    proxy, _folder, lines = traced
    with pytest.raises(RuntimeError, match="bad: why"):
        proxy.Fail("why")
    line = next(line for line in lines if "Fail(" in line)
    assert "API folder#1.Fail('why') raised RuntimeError: bad: why" in line
    assert "Traceback (most recent call last)" in line
    # Missing attributes still raise AttributeError, so hasattr() keeps working.
    assert not hasattr(proxy, "NoSuchCall")


def test_long_values_are_truncated(traced):
    proxy, _folder, lines = traced
    assert len(proxy.GetLong()) == 5000  # the caller still gets the whole value
    line = next(line for line in lines if "GetLong" in line)
    assert "... (3002 more chars)" in line and len(line) < 2200


def test_redact_hides_secrets_recursively():
    config = {
        "engine": ["py", "-m", "x"],
        "anthropic_api_key": "sk-1",
        "nested": {"Token": "t", "keep_work_dir": True},
        "client_secret": "s",
    }
    assert debug_log.redact(config) == {
        "engine": ["py", "-m", "x"],
        "anthropic_api_key": debug_log.REDACTED,
        "nested": {"Token": debug_log.REDACTED, "keep_work_dir": True},
        "client_secret": debug_log.REDACTED,
    }


def test_enabled_by_config_or_environment():
    assert not debug_log.enabled({}, environ={})
    assert not debug_log.enabled({"debug": False}, environ={})
    assert debug_log.enabled({"debug": True}, environ={})
    assert debug_log.enabled({}, environ={"BETTER_CAPTIONS_DEBUG": "1"})


def test_old_logs_are_pruned(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    for n in range(25):
        (logs / ("better-captions-20260101-0000%02d.log" % n)).write_text("old")
    (logs / "unrelated.txt").write_text("keep")
    log = debug_log.start({"debug": True, "_path": str(tmp_path / "config.json")}, environ={})
    try:
        names = sorted(os.listdir(logs))
        assert len([n for n in names if n.endswith(".log")]) == debug_log.KEEP
        assert "unrelated.txt" in names and "better-captions-20260101-000000.log" not in names
        assert os.path.dirname(log.path) == str(logs)
    finally:
        debug_log.finish()


# ---------------------------------------------------------------------------
# A whole run


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "config.json"
    path.write_text("{}")
    return {
        "engine": [sys.executable, "-I", FAKE_ENGINE],
        "model": "fake-model",
        "naming_provider": "heuristic",
        "anthropic_api_key": "sk-secret-value",
        "_path": str(path),
    }


def _logs(tmp_path):
    folder = tmp_path / "logs"
    return sorted(folder.iterdir()) if folder.is_dir() else []


def test_debug_run_writes_a_full_log(config, tmp_path):
    config["debug"] = True
    resolve = make_world()
    backend = FakeBackend(accept_all(subtitles=True, textplus=True))
    outcome = app.main(resolve=resolve, backend=backend, config=config, environ={}, sleep=lambda s: None)

    assert outcome.error is None, backend.errors
    assert debug_log.current() is None  # closed at the end of the run
    (log_path,) = _logs(tmp_path)
    assert log_path.name.startswith("better-captions-") and log_path.suffix == ".log"
    text = log_path.read_text(encoding="utf-8")
    assert "plugin version: 0.1.0" in text and "python executable: %s" % sys.executable in text
    assert "Resolve: DaVinci Resolve 20.0.0" in text and "UI backend: FakeBackend" in text
    assert "sk-secret-value" not in text and "'anthropic_api_key': '***redacted***'" in text
    # Resolve API calls, with nested objects traced too.
    assert "API resolve#1.GetProjectManager() -> <ProjectManager#2>" in text
    assert "API ProjectManager#2.GetCurrentProject() -> <CurrentProject#3>" in text
    assert ".SetRenderSettings({'TargetDir': " in text and ".AppendToTimeline(" in text
    # Render diagnostics before the render.
    assert "===== Render diagnostics =====" in text
    assert "GetRenderFormats() -> {'QuickTime': 'mov', 'MP4': 'mp4', 'Wave': 'wav'}" in text
    assert "GetRenderCodecs('Wave') -> {'Linear PCM': 'LinearPCM'}" in text
    assert "GetRenderCodecs('wav') -> {}" in text
    assert "GetRenderPresetList() is not available" in text  # the fake has no such call
    assert text.index("End of render diagnostics") < text.index("SetCurrentRenderFormatAndCodec(")
    # The engine: command with --verbose, environment changes, raw output and exit code.
    assert "command: [" in text and "'--verbose']" in text
    assert "env set: PYTHONUNBUFFERED=1" in text
    assert "engine stderr: warning: downloading model (fake)" in text
    assert "engine stdout: not json: model loaded" in text and 'engine stdout: {"event": "done"' in text
    assert "engine exit: code 0" in text
    assert "Review result: ReviewResult(" in text
    assert "===== Outcome =====" in text and 'Created subtitle track 1 "Maria"' in text
    # The summary points at the log.
    assert backend.messages[0][1].endswith("\n\nDebug log: %s" % log_path)


def test_debug_run_with_an_error_logs_the_traceback(config, tmp_path):
    config["engine_args"] = ["--fail"]
    backend = FakeBackend(accept_all())
    outcome = app.main(
        resolve=make_world(),
        backend=backend,
        config=config,
        environ={"BETTER_CAPTIONS_DEBUG": "1"},
        sleep=lambda s: None,
    )
    (log_path,) = _logs(tmp_path)
    text = log_path.read_text(encoding="utf-8")
    assert "Error:\nTraceback (most recent call last)" in text and "EngineError: RuntimeError: no speech found" in text
    assert "engine exit: code 1" in text
    assert backend.errors == [("Better Captions", "%s\n\nDebug log: %s" % (outcome.error, log_path))]


def test_debug_off_writes_no_log_and_wraps_nothing(config, tmp_path, monkeypatch):
    def no_tracing(*args, **kwargs):
        raise AssertionError("nothing may be wrapped with debug off")

    monkeypatch.setattr(debug_log.DebugLog, "wrap", no_tracing)
    backend = FakeBackend(accept_all())
    outcome = app.main(resolve=make_world(), backend=backend, config=config, environ={}, sleep=lambda s: None)
    assert outcome.error is None, backend.errors
    assert _logs(tmp_path) == []
    assert "Debug log" not in backend.messages[0][1]


@pytest.mark.parametrize("highlight", [False, True])
def test_debug_on_does_not_change_the_result(config, tmp_path, highlight):
    """The proxy is transparent: the same run gives the same tracks with debug on and off."""
    config["engine_args"] = ["--with-words"]

    def run(debug):
        resolve = make_world()
        backend = FakeBackend(accept_all(subtitles=True, textplus=True, highlight_words=highlight))
        app.main(resolve=resolve, backend=backend, config=dict(config, debug=debug), environ={}, sleep=lambda s: None)
        tl = resolve.project.timeline
        tracks = {
            kind: [(t.name, [(i.GetStart(), i.GetName()) for i in t.items]) for t in tl.tracks[kind]]
            for kind in ("subtitle", "video")
        }
        return tracks, backend.messages[0][1].split("\n\nDebug log:")[0], backend.errors

    assert run(False) == run(True)
