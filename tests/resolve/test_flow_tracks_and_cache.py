"""app.run with the audio track picker and the render cache wired in."""

import json
import os
import sys

import pytest

from better_captions_plugin import app
from tests.resolve.fake_resolve import FakeTrack, make_world
from tests.resolve.fake_ui import FakeBackend, accept_all

FAKE_ENGINE = os.path.join(os.path.dirname(__file__), "fake_engine.py")


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


def _run(resolve, backend, config):
    return app.main(resolve=resolve, backend=backend, config=config, sleep=lambda s: None)


def _project(resolve):
    return resolve.GetProjectManager().GetCurrentProject()


def _timeline(resolve):
    return _project(resolve).GetCurrentTimeline()


def _two_track_world():
    resolve = make_world()
    _timeline(resolve).tracks["audio"].append(FakeTrack("Music"))
    return resolve


def test_one_audio_track_skips_the_picker(config):
    backend = FakeBackend(accept_all(subtitles=True, textplus=False))
    outcome = _run(make_world(), backend, config)
    assert outcome.error is None and not outcome.cancelled
    assert backend.track_requests == []


def test_picked_tracks_are_the_only_ones_rendered_and_restored(config, monkeypatch):
    resolve = _two_track_world()
    timeline = _timeline(resolve)
    seen = []
    real_render = app.resolve_io.render_audio

    def spy(project, tl, *args, **kwargs):
        seen.append(tl.enabled_audio())
        return real_render(project, tl, *args, **kwargs)

    monkeypatch.setattr(app.resolve_io, "render_audio", spy)
    backend = FakeBackend(accept_all(subtitles=True, textplus=False), tracks_answer=[1])
    outcome = _run(resolve, backend, config)
    assert outcome.error is None and not outcome.cancelled
    assert seen == [[1]]
    assert timeline.enabled_audio() == [1, 2]
    with open(config["_path"]) as f:
        saved = json.load(f)
    assert list(saved["audio_tracks"].values()) == [[1]]


def test_cancelling_the_picker_cancels_the_run(config):
    backend = FakeBackend(accept_all(subtitles=True, textplus=False), tracks_answer=None)
    outcome = _run(_two_track_world(), backend, config)
    assert outcome.cancelled
    assert backend.progress_views == []


def _count_renders(monkeypatch):
    calls = []
    real_render = app.resolve_io.render_audio

    def spy(*args, **kwargs):
        calls.append(1)
        return real_render(*args, **kwargs)

    monkeypatch.setattr(app.resolve_io, "render_audio", spy)
    return calls


def test_unchanged_timeline_reuses_the_render(config, monkeypatch):
    calls = _count_renders(monkeypatch)
    resolve = make_world()
    for _ in range(2):
        backend = FakeBackend(accept_all(subtitles=True, textplus=False))
        assert _run(resolve, backend, config).error is None
    assert len(calls) == 1
    assert any("Reusing previous audio render" in line for line in backend.progress_views[0].lines)


def test_changed_track_selection_or_force_renders_again(config, monkeypatch):
    calls = _count_renders(monkeypatch)
    resolve = _two_track_world()
    for pick in ([1, 2], [1], [1]):
        _run(resolve, FakeBackend(accept_all(subtitles=True, textplus=False), tracks_answer=pick), config)
    assert len(calls) == 2
    config["force_rerender"] = True
    _run(resolve, FakeBackend(accept_all(subtitles=True, textplus=False), tracks_answer=[1]), config)
    assert len(calls) == 3


def test_unchanged_track_choice_is_not_a_warning(config):
    resolve = _two_track_world()
    for _ in range(2):
        outcome = _run(resolve, FakeBackend(accept_all(subtitles=True, textplus=False), tracks_answer=[1]), config)
        assert outcome.error is None and outcome.warnings == []


def test_pinned_track_never_reuses_the_mix_when_resolve_reports_stale_enabled_states(config, monkeypatch):
    # Resolve can keep answering GetIsTrackEnabled -> True after SetTrackEnable(False) succeeded; the
    # one-track stem must still get its own render, not the cached mix (which pinned everyone to it).
    calls = _count_renders(monkeypatch)
    resolve = _two_track_world()
    _timeline(resolve).enabled_reads_stale = True
    backend = FakeBackend(
        accept_all(subtitles=True, textplus=False), tracks_answer=[1, 2], speakers_answer={1: "Anthony"}
    )
    assert _run(resolve, backend, config).error is None
    assert len(calls) == 2
    engine_line = next(line for line in backend.progress_views[0].lines if line.startswith("Engine: "))
    argv = engine_line.split(" ")
    mix, stem = argv[argv.index("run") + 1], argv[argv.index("--fixed-track") + 1].partition("=")[2]
    assert mix != stem
