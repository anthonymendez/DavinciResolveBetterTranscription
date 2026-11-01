"""The progress window's bar and percentage text, on both backends, driven through fakes (no windows)."""

import pytest

from better_captions_plugin.ui.uimanager_ui import (
    BAR_EMPTY,
    BAR_FILLED,
    BAR_WIDTH,
    UIManagerBackend,
    progress_bar_text,
)
from tests.resolve.test_ui import FakeBmd, FakeFusion

# --- UIManager: a Label of block characters


def test_bar_text_fills_proportionally_and_clamps():
    assert progress_bar_text(0) == BAR_EMPTY * BAR_WIDTH
    assert progress_bar_text(100) == BAR_FILLED * BAR_WIDTH
    assert progress_bar_text(50) == BAR_FILLED * (BAR_WIDTH // 2) + BAR_EMPTY * (BAR_WIDTH // 2)
    assert progress_bar_text(-5) == progress_bar_text(0)
    assert progress_bar_text(250) == progress_bar_text(100)
    assert all(len(progress_bar_text(p)) == BAR_WIDTH for p in (0, 1, 33.3, 99.9, 100, None))


@pytest.mark.parametrize("bad", [None, "", "abc", object()])
def test_bar_text_for_none_or_garbage_is_empty(bad):
    assert progress_bar_text(bad) == BAR_EMPTY * BAR_WIDTH


def test_uimanager_bar_and_percent_follow_set_progress():
    bmd = FakeBmd()
    view = UIManagerBackend(FakeFusion(), bmd).progress("t")
    items = bmd.dispatcher.windows[0].GetItems()
    assert items["Bar"].Text == progress_bar_text(None) and items["Percent"].Text == ""
    view.set_progress(25.7)
    assert items["Percent"].Text == "25%" and items["Bar"].Text == progress_bar_text(25.7)
    assert items["Bar"].Text.count(BAR_FILLED) == 10
    view.set_progress(100)
    assert items["Percent"].Text == "100%" and items["Bar"].Text == BAR_FILLED * BAR_WIDTH
    view.set_progress(None)
    assert items["Percent"].Text == "" and items["Bar"].Text == BAR_EMPTY * BAR_WIDTH
    view.set_progress(130)
    assert items["Percent"].Text == "100%" and items["Bar"].Text == BAR_FILLED * BAR_WIDTH


def test_uimanager_progress_survives_a_missing_bar_widget():
    bmd = FakeBmd()
    view = UIManagerBackend(FakeFusion(), bmd).progress("t")
    del bmd.dispatcher.windows[0].GetItems()["Bar"]  # e.g. a UIManager that rejects the label
    view.set_progress(40)
    assert bmd.dispatcher.windows[0].GetItems()["Percent"].Text == "40%"


# --- Tk: ttk.Progressbar + percentage label, on stand-ins so no Tk root or window exists


class FakeBar:
    def __init__(self):
        self.mode, self.value, self.running, self.calls = "determinate", 0.0, False, []

    def configure(self, **kwargs):
        self.mode = kwargs.get("mode", self.mode)
        self.calls.append(("configure", kwargs))

    def __setitem__(self, key, value):
        assert key == "value"
        self.value = value

    def start(self, interval=50):
        self.running = True
        self.calls.append(("start", interval))

    def stop(self):
        self.running = False
        self.calls.append(("stop",))


class FakeVar:
    def __init__(self):
        self.value = ""

    def set(self, value):
        self.value = value


class FakeRoot:
    def update(self):
        pass


def _tk_view():
    pytest.importorskip("tkinter")
    from better_captions_plugin.ui import tk_ui

    view = tk_ui.TkProgress.__new__(tk_ui.TkProgress)  # skip __init__: it builds real widgets
    view._root, view._bar, view._percent, view._indeterminate = FakeRoot(), FakeBar(), FakeVar(), False
    return view


def test_tk_bar_value_and_percent_text_for_numbers():
    view = _tk_view()
    view.set_progress(42.9)
    assert view._bar.value == pytest.approx(42.9) and view._percent.value == "42%"
    view.set_progress(150)
    assert view._bar.value == 100.0 and view._percent.value == "100%"
    view.set_progress(-3)
    assert view._bar.value == 0.0 and view._percent.value == "0%"
    assert view._bar.mode == "determinate" and not view._bar.running


def test_tk_none_is_indeterminate_and_a_number_switches_back():
    view = _tk_view()
    view.set_progress(10)
    view.set_progress(None)
    assert view._bar.mode == "indeterminate" and view._bar.running and view._percent.value == ""
    view.set_progress(None)  # already sweeping: not restarted
    assert [c for c in view._bar.calls if c[0] == "start"] == [("start", 60)]
    view.set_progress(55)
    assert view._bar.mode == "determinate" and not view._bar.running
    assert view._bar.value == 55 and view._percent.value == "55%"
