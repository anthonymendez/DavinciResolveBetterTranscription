"""app.main on the Resolve versions/editions compat.py tells apart (21.1 moved Python scripting to Studio)."""

import os
import subprocess
import sys

import pytest

from better_captions_plugin import app, compat, resolve_io
from tests.resolve.fake_resolve import make_world
from tests.resolve.fake_ui import FakeBackend, accept_all

FAKE_ENGINE = os.path.join(os.path.dirname(__file__), "fake_engine.py")
PLUGIN = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "resolve_plugin")


@pytest.fixture
def config():
    return {"engine": [sys.executable, "-I", FAKE_ENGINE], "model": "fake-model", "naming_provider": "heuristic"}


def _run(resolve, backend, config, **kwargs):
    kwargs.setdefault("sleep", lambda s: None)
    return app.main(resolve=resolve, backend=backend, config=config, **kwargs)


def _assert_nothing_done(backend, outcome):
    assert backend.progress_views == [] and backend.requests == [] and backend.messages == []
    assert outcome.created == [] and outcome.work_dir is None


def test_no_resolve_object_says_python_needs_studio(config):
    backend = FakeBackend(accept_all())
    outcome = _run(None, backend, config)
    assert outcome.error == compat.NO_RESOLVE_MESSAGE
    assert backend.errors == [("Better Captions", compat.NO_RESOLVE_MESSAGE)]
    _assert_nothing_done(backend, outcome)


def test_no_resolve_object_without_a_backend_is_printed(config, capsys):
    outcome = app.main(resolve=None, config=config)
    assert outcome.error == compat.NO_RESOLVE_MESSAGE
    assert compat.NO_RESOLVE_MESSAGE in capsys.readouterr().out


def test_free_21_1_is_gated_before_anything_runs(config, monkeypatch):
    resolve = make_world(product="DaVinci Resolve")
    resolve.version = "21.1.2.0005"
    monkeypatch.setattr(app, "choose_backend", lambda *a, **k: pytest.fail("no UI before the Studio check"))
    backend = FakeBackend(accept_all())
    outcome = _run(resolve, backend, config)
    expected = compat.gated_message(compat.detect(resolve))
    assert outcome.error == expected and "DaVinci Resolve 21.1.2" in expected and "Studio" in expected
    assert backend.errors == [("Better Captions", expected)]
    _assert_nothing_done(backend, outcome)
    assert resolve.project.timeline.GetTrackCount("subtitle") == 0
    assert resolve.project.render_settings == {} and resolve.project.jobs == {}


def test_free_21_1_without_a_backend_is_printed_not_rendered_with_uimanager(config, monkeypatch, capsys):
    resolve = make_world(product="DaVinci Resolve")
    resolve.version = "21.1.2.0005"
    monkeypatch.setattr(app, "choose_backend", lambda *a, **k: pytest.fail("no UI outside Resolve's script host"))
    outcome = app.main(resolve=resolve, config=config)
    assert "needs DaVinci Resolve Studio" in outcome.error
    assert outcome.error in capsys.readouterr().out


def test_studio_21_1_2_runs_the_full_flow_without_a_version_warning(config):
    resolve = make_world(product="DaVinci Resolve Studio")
    resolve.version = "21.1.2.0005"
    backend = FakeBackend(accept_all(subtitles=True))
    outcome = _run(resolve, backend, config)
    assert outcome.error is None and not outcome.cancelled, backend.errors
    assert outcome.warnings == []
    assert backend.progress_views[0].lines[0] == "Resolve: DaVinci Resolve Studio 21.1.2"
    assert [t.name for t in resolve.project.timeline.tracks["subtitle"]] == ["Maria", "SPEAKER_01"]
    assert "Warning:" not in backend.messages[0][1]


def test_free_21_0_still_runs(config):
    resolve = make_world(product="DaVinci Resolve")
    resolve.version = "21.0.4.0009"
    backend = FakeBackend(accept_all(subtitles=True))
    outcome = _run(resolve, backend, config)
    assert outcome.error is None and outcome.warnings == [], backend.errors
    assert backend.progress_views[0].lines[0] == "Resolve: DaVinci Resolve 21.0.4"


def test_newer_than_tested_studio_gets_a_warning(config):
    resolve = make_world(product="DaVinci Resolve Studio")
    resolve.version = "22.0.0.0001"
    backend = FakeBackend(accept_all(subtitles=True))
    outcome = _run(resolve, backend, config)
    assert outcome.error is None, backend.errors
    assert len(outcome.warnings) == 1 and "DaVinci Resolve Studio 22.0.0 is newer" in outcome.warnings[0]
    assert "Warning: DaVinci Resolve Studio 22.0.0 is newer" in backend.messages[0][1]
    assert outcome.created


class _DeadApiResolve:
    """What free 21.1+ might hand a Python script: an object whose calls all return None."""

    def __getattr__(self, name):
        return lambda *a, **k: None


def test_free_with_no_project_manager_mentions_studio(config):
    resolve = make_world(product="DaVinci Resolve")
    resolve.version = None  # GetVersionString() -> None: version unknown
    resolve.GetProjectManager = lambda: None
    backend = FakeBackend(accept_all())
    outcome = _run(resolve, backend, config)
    assert outcome.error == compat.NO_RESOLVE_MESSAGE and "Studio" in outcome.error
    assert "open a project" not in outcome.error
    assert backend.errors == [("Better Captions", outcome.error)]
    assert backend.requests == []


def test_dead_api_object_mentions_studio(config):
    backend = FakeBackend(accept_all())
    outcome = _run(_DeadApiResolve(), backend, config)
    assert outcome.error == compat.NO_RESOLVE_MESSAGE
    assert backend.progress_views == []


def test_studio_without_project_manager_is_not_blamed_on_the_edition():
    resolve = make_world(product="DaVinci Resolve Studio")
    resolve.GetProjectManager = lambda: None
    with pytest.raises(resolve_io.ResolveError) as exc:
        resolve_io.current_project_and_timeline(resolve)
    assert "project manager" in str(exc.value) and "Studio" not in str(exc.value)


def test_no_open_project_on_a_known_version_still_says_open_a_project():
    resolve = make_world(product="DaVinci Resolve")
    resolve.GetProjectManager = lambda: type("M", (), {"GetCurrentProject": lambda self: None})()
    with pytest.raises(resolve_io.ResolveError, match=r"^open a project first$"):
        resolve_io.current_project_and_timeline(resolve)


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="needs a display-less tkinter to stay headless")
def test_launcher_without_resolve_globals_explains_the_studio_requirement(tmp_path):
    """Resolve free 21.1+ may run the script without a `resolve` global at all."""
    # Copy just the launcher next to a link to src (like test_plugin_zip's fallback test), so the
    # current source runs rather than the committed zip.
    utility = tmp_path / "resolve_plugin" / "Utility"
    utility.mkdir(parents=True)
    os.symlink(os.path.join(PLUGIN, "src"), str(tmp_path / "resolve_plugin" / "src"))
    with open(os.path.join(PLUGIN, "Utility", "Better Captions.py"), "rb") as handle:
        (utility / "Better Captions.py").write_bytes(handle.read())
    home = tmp_path / "home"
    home.mkdir()
    (home / "config.json").write_text('{"engine": ["true"]}')
    driver = tmp_path / "driver.py"
    # No init_globals: `resolve`, `fusion` and `bmd` are all missing.
    driver.write_text("import runpy\nrunpy.run_path(%r, run_name='__main__')\n" % str(utility / "Better Captions.py"))
    env = {k: v for k, v in os.environ.items() if k not in ("DISPLAY", "WAYLAND_DISPLAY")}
    env["BETTER_CAPTIONS_HOME"] = str(home)
    result = subprocess.run([sys.executable, str(driver)], capture_output=True, text=True, env=env, cwd=str(tmp_path))
    assert result.returncode == 0, result.stderr
    assert "Python scripts only work in DaVinci Resolve Studio" in result.stdout, result.stdout + result.stderr
