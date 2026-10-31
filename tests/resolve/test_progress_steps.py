"""The progress window's step list (done / current / pending) and how the flow drives it."""

import os
import sys

import pytest

from better_captions_plugin import app, engine_runner
from better_captions_plugin.ui import (
    STEP_CURRENT,
    STEP_DONE,
    STEP_MARKERS,
    STEP_PENDING,
    ProgressView,
    step_states,
)
from better_captions_plugin.ui.uimanager_ui import UIManagerBackend
from tests.resolve.conftest import skip_unless_gui_tests
from tests.resolve.fake_resolve import make_world
from tests.resolve.fake_ui import FakeBackend, accept_all
from tests.resolve.test_ui import FakeBmd, FakeFusion

FAKE_ENGINE = os.path.join(os.path.dirname(__file__), "fake_engine.py")


@pytest.fixture
def config():
    return {"engine": [sys.executable, "-I", FAKE_ENGINE], "model": "fake-model", "naming_provider": "heuristic"}


def test_step_states_mark_done_current_pending():
    assert step_states(4, 1) == [STEP_DONE, STEP_CURRENT, STEP_PENDING, STEP_PENDING]
    assert step_states(3, None) == [STEP_PENDING] * 3
    assert step_states(3, 3) == [STEP_DONE] * 3  # past the last step: everything is done


def test_base_progress_view_ignores_steps_and_is_never_paused():
    view = ProgressView()
    view.set_steps(["a", "b"])
    view.set_step(1)
    assert view.paused is False and view.cancelled is False
    assert view.step_lines() == ["%s  a" % STEP_MARKERS[STEP_DONE], "%s  b" % STEP_MARKERS[STEP_CURRENT]]


def test_step_labels_cover_render_engine_stages_load_review_and_tracks():
    assert app.STEP_LABELS[0] == "Render audio"
    engine = [engine_runner.stage_label(stage) for stage in engine_runner.STAGES]
    assert app.STEP_LABELS[app.STEP_ENGINE : app.STEP_LOAD] == engine
    assert app.STEP_LABELS[app.STEP_LOAD :] == ["Load transcript", "Review speakers", "Create tracks"]
    assert (
        0,
        len(app.STEP_LABELS) - 2,
        len(app.STEP_LABELS) - 1,
    ) == (app.STEP_RENDER, app.STEP_REVIEW, app.STEP_TRACKS)


def test_uimanager_progress_lists_steps_with_markers():
    bmd = FakeBmd()
    view = UIManagerBackend(FakeFusion(), bmd).progress("t")
    items = bmd.dispatcher.windows[0].GetItems()
    view.set_steps(["Render audio", "Transcribing", "Create tracks"])
    done, current, pending = (STEP_MARKERS[s] for s in (STEP_DONE, STEP_CURRENT, STEP_PENDING))
    assert items["Steps"].Text == "\n".join("%s  %s" % (pending, label) for label in view.step_labels)
    view.set_step(1)
    assert items["Steps"].Text.split("\n") == [
        "%s  Render audio" % done,
        "%s  Transcribing" % current,
        "%s  Create tracks" % pending,
    ]
    view.set_step(3)
    assert items["Steps"].Text.split("\n") == ["%s  %s" % (done, label) for label in view.step_labels]


def test_flow_advances_the_steps_through_a_run(config):
    backend = FakeBackend(accept_all(subtitles=True, textplus=False))
    outcome = app.main(resolve=make_world(), backend=backend, config=config, sleep=lambda s: None)
    assert outcome.error is None and not outcome.cancelled, backend.errors

    # First window: render, engine stages, load transcript, up to the review (its own window).
    view, tracks_view = backend.progress_views
    assert list(view.step_labels) == app.STEP_LABELS
    indexes = [index for index, _paused in view.step_log]
    assert indexes[0] == app.STEP_RENDER and indexes[-1] == app.STEP_REVIEW
    assert indexes == sorted(set(indexes))  # forward only, each step once (transcribe reports twice)
    assert app.STEP_LOAD in indexes and app.STEP_ENGINE in indexes
    assert {app.STEP_ENGINE + engine_runner.STAGES.index(s) for s in ("transcribe", "diarize", "align")} <= set(indexes)
    assert view.closed
    # The marker lines the user sees at the end of that window: everything before the review done.
    lines = view.step_lines()
    assert lines[0].startswith(STEP_MARKERS[STEP_DONE]) and lines[app.STEP_REVIEW].startswith(
        STEP_MARKERS[STEP_CURRENT]
    )
    assert lines[app.STEP_TRACKS].startswith(STEP_MARKERS[STEP_PENDING])

    # Second window while the tracks are made, finishing with every step done.
    assert [index for index, _paused in tracks_view.step_log] == [app.STEP_TRACKS, len(app.STEP_LABELS)]
    assert all(line.startswith(STEP_MARKERS[STEP_DONE]) for line in tracks_view.step_lines())
    assert tracks_view.closed and "Creating tracks…" in tracks_view.statuses


def test_cancelled_review_leaves_the_tracks_step_pending(config):
    backend = FakeBackend(answer=None)
    outcome = app.main(resolve=make_world(), backend=backend, config=config, sleep=lambda s: None)
    assert outcome.cancelled
    assert len(backend.progress_views) == 1 and backend.progress_views[0].step_index == app.STEP_REVIEW


# --- tkinter (needs a display; opt-in, see conftest.py)


def test_tk_progress_lists_steps_and_pause_button():
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
        view.set_steps(["Render audio", "Transcribing"])
        view.set_step(1)
        assert [str(label.cget("text")) for label in view._step_labels] == view.step_lines()
        assert view.paused is False
        view._toggle_pause()
        assert view.paused is True and str(view._pause_button.cget("text")) == "Resume"
        view._cancel()
        assert view.cancelled and view.paused is False
        view.close()
    finally:
        root.destroy()
