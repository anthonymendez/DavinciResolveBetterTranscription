"""The review dialog stays a bounded height with many speakers (Tk scroll area, UIManager paging); fakes only."""

from types import SimpleNamespace as NS

import pytest

from better_captions_plugin.ui import ReviewRequest, SpeakerRow, review_scroll
from better_captions_plugin.ui.uimanager_ui import UIManagerBackend
from tests.resolve.test_ui import FakeBmd, FakeFusion

PER_PAGE = review_scroll.VISIBLE_ROWS


def _request(count):
    return ReviewRequest(
        speakers=[SpeakerRow("S%02d" % i, "S%02d" % i, 3, "line %d" % i) for i in range(count)],
        templates=["Style A", "Style B", "Style C"],
        timeline_name="T",
    )


# --- pure helpers


def test_bounded_height_caps_at_visible_rows():
    assert review_scroll.bounded_height(3, 30) == 90
    assert review_scroll.bounded_height(25, 30) == PER_PAGE * 30
    assert review_scroll.bounded_height(25, 30, visible=4) == 120
    assert review_scroll.bounded_height(0, 30) == 0


def test_paging_math():
    assert review_scroll.page_count(0) == 1 and review_scroll.page_count(10) == 1
    assert review_scroll.page_count(11) == 2 and review_scroll.page_count(25) == 3
    assert review_scroll.page_bounds(0, 25) == (0, 10)
    assert review_scroll.page_bounds(2, 25) == (20, 25)
    assert review_scroll.page_bounds(9, 25) == (20, 25)  # clamped
    assert review_scroll.clamp_page(-3, 25) == 0
    assert review_scroll.page_label(1, 25) == "Speakers 11–20 of 25"


def test_wheel_units_for_windows_mac_and_x11():
    assert review_scroll.wheel_units(NS(delta=120, num=None)) == -1  # wheel up scrolls up
    assert review_scroll.wheel_units(NS(delta=-240, num=None)) == 2
    assert review_scroll.wheel_units(NS(delta=-1, num=None)) == 1  # macOS small deltas
    assert review_scroll.wheel_units(NS(delta=0, num=4)) == -1
    assert review_scroll.wheel_units(NS(delta=0, num=5)) == 1
    assert review_scroll.wheel_units(NS(delta=0, num=None)) == 0


# --- UIManager: pages of rows, edits kept across them


def _name_widgets(window):
    return sorted(key for key in window.GetItems() if key.startswith("Name_"))


def test_uimanager_few_speakers_have_no_pager():
    seen = {}

    def script(window):
        seen["items"] = window.GetItems()
        window.On.Ok.Clicked()

    result = UIManagerBackend(FakeFusion(), FakeBmd(script)).review(_request(PER_PAGE))
    assert "NextPage" not in seen["items"] and "PrevPage" not in seen["items"]
    assert len(result.names) == PER_PAGE


def test_uimanager_many_speakers_page_with_bounded_window_and_keep_edits():
    visited = []

    def script(window):
        items = window.GetItems()
        page = len(visited)
        visited.append(_name_widgets(window))
        if page == 0:
            items["Name_S00"].Text = "Alice"
            items["Template_S01"].CurrentIndex = 2
            assert items["PrevPage"].Enabled is False
            window.On.NextPage.Clicked()
        elif page == 1:  # second page: S10..S19
            items["Name_S10"].Text = " Bob "
            window.On.PrevPage.Clicked()
        elif page == 2:  # back on the first page: edits are still there
            assert items["Name_S00"].Text == "Alice" and items["Template_S01"].CurrentIndex == 2
            window.On.NextPage.Clicked()
        elif page == 3:
            assert items["Name_S10"].Text == " Bob "
            window.On.NextPage.Clicked()
        else:  # last page: S20..S24
            assert items["NextPage"].Enabled is False
            items["Name_S24"].Text = "Zed"
            window.On.Ok.Clicked()

    bmd = FakeBmd(script)
    result = UIManagerBackend(FakeFusion(), bmd).review(_request(25))

    assert [len(page) for page in visited] == [10, 10, 10, 10, 5]  # never more than a page of rows
    assert visited[0][0] == "Name_S00" and visited[1][0] == "Name_S10" and visited[4][-1] == "Name_S24"
    heights = [w.props["Geometry"][3] for w in bmd.dispatcher.windows]
    assert max(heights) <= 240 + 48 * PER_PAGE + 40  # bounded, however many speakers
    assert result.names["S00"] == "Alice" and result.names["S10"] == "Bob" and result.names["S24"] == "Zed"
    assert result.names["S15"] == "S15"  # never visited after paging past: untouched default
    assert len(result.names) == 25 and len(result.template_index) == 25
    assert result.template_index["S01"] == 2
    assert result.template_index["S12"] == 2  # default: row index capped at the last template


def test_uimanager_paging_then_cancel_returns_none():
    def script(window):
        if "NextPage" in window.GetItems() and not script.paged:
            script.paged = True
            window.On.NextPage.Clicked()
        else:
            window.On.Cancel.Clicked()

    script.paged = False
    assert UIManagerBackend(FakeFusion(), FakeBmd(script)).review(_request(25)) is None


# --- Tk: canvas sizing and mouse wheel, on stand-ins (no Tk root or window)


class FakeCanvas:
    def __init__(self):
        self.config, self.scrolled, self.moved = {}, [], []

    def configure(self, **kwargs):
        self.config.update(kwargs)

    def yview_scroll(self, units, what):
        self.scrolled.append((units, what))

    def yview_moveto(self, fraction):
        self.moved.append(fraction)


class FakeScrollbar:
    def __init__(self):
        self.packed = False

    def pack(self, **kwargs):
        self.packed = True

    def pack_forget(self):
        self.packed = False


def _tk_dialog(speaker_count, row_height=30):
    pytest.importorskip("tkinter")
    from better_captions_plugin.ui import tk_ui

    dialog = tk_ui._ReviewDialog.__new__(tk_ui._ReviewDialog)
    lines = speaker_count + 1
    dialog.speakers = [object()] * speaker_count
    dialog.table = NS(
        update_idletasks=lambda: None, winfo_reqwidth=lambda: 700, winfo_reqheight=lambda: row_height * lines
    )
    dialog._canvas, dialog._scrollbar, dialog._scrolls = FakeCanvas(), FakeScrollbar(), False
    return dialog


def test_tk_rows_area_is_bounded_and_scrollbar_appears_only_when_needed():
    many = _tk_dialog(25)
    many._fit_table()
    assert many._canvas.config["height"] == (PER_PAGE + 1) * 30  # header + visible rows
    assert many._canvas.config["scrollregion"] == (0, 0, 700, 26 * 30)
    assert many._canvas.config["yscrollincrement"] == 30
    assert many._scrollbar.packed and many._scrolls

    few = _tk_dialog(3)
    few._fit_table()
    assert few._canvas.config["height"] == 4 * 30 and not few._scrollbar.packed and not few._scrolls

    many.speakers = many.speakers[:3]  # rows rebuilt with fewer speakers (after "Fix speakers…")
    many.table = few.table
    many._fit_table()
    assert not many._scrollbar.packed and not many._scrolls


def test_tk_mouse_wheel_scrolls_only_when_the_rows_overflow():
    dialog = _tk_dialog(25)
    dialog._fit_table()
    dialog._on_wheel(NS(delta=-120, num=None))
    dialog._on_wheel(NS(delta=0, num=4))
    assert dialog._canvas.scrolled == [(1, "units"), (-1, "units")]

    short = _tk_dialog(3)
    short._fit_table()
    short._on_wheel(NS(delta=-120, num=None))
    assert short._canvas.scrolled == []
