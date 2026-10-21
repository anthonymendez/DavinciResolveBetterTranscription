"""UI backends: UIManager against a fake Fusion UI; tkinter only when a display is available."""

import sys
import pytest

from better_captions_plugin.ui import ReviewRequest, ReviewResult, SpeakerRow, choose_backend
from better_captions_plugin.ui.uimanager_ui import UIManagerBackend

from tests.resolve.fake_resolve import make_world


# --- a minimal fake of fusion.UIManager / bmd.UIDispatcher


class Element:
    def __init__(self, props):
        self.props = dict(props)
        self.items = []
        self.Text = props.get("Text", "")
        self.PlainText = props.get("PlainText", "")
        self.Checked = props.get("Checked", False)
        self.Enabled = props.get("Enabled", True)
        self.CurrentIndex = 0
        self.ID = props.get("ID")
        self.rows = []  # Tree rows
        self.header = None
        self.current = None  # Tree current row
        self.selected = []  # Tree selected rows
        self.ColumnWidth = {}

    def AddItem(self, label):
        self.items.append(label)

    def Clear(self):  # ComboBox and Tree
        self.items, self.rows, self.current, self.selected = [], [], None, []
        self.CurrentIndex = -1

    def NewItem(self):
        return TreeItem()

    def AddTopLevelItem(self, item):
        self.rows.append(item)

    def SetHeaderItem(self, item):
        self.header = item

    def CurrentItem(self):
        return self.current

    def SelectedItems(self):  # the Fusion bridge returns Lua tables as dicts keyed 1..n
        return {index: item for index, item in enumerate(self.selected, start=1)}

    def row(self, text):
        return next(item for item in self.rows if item.Text[0] == text)

    def texts(self, column=0):
        return [item.Text[column] for item in self.rows]


class TreeItem:
    def __init__(self):
        self.Text = {}


class Handlers:
    def __init__(self):
        self.__dict__["_h"] = {}

    def __setattr__(self, key, value):
        self._h[key] = value

    def __getattr__(self, key):
        return self._h.get(key)


class On:
    def __init__(self):
        self.slots = {}

    def __getattr__(self, key):
        return self.__getitem__(key)

    def __getitem__(self, key):
        return self.slots.setdefault(key, Handlers())


class Window:
    def __init__(self, props, children, elements):
        self.props = props
        self.On = On()
        self.elements = elements
        self.shown = False

    def GetItems(self):
        return self.elements

    def Show(self):
        self.shown = True

    def Hide(self):
        self.shown = False


class FakeUIManager:
    def __init__(self):
        self.elements = {}

    def _widget(self, props=None, children=None):
        element = Element(props or {})
        if element.ID:
            self.elements[element.ID] = element
        return element

    def __getattr__(self, name):  # ui.Label, ui.Button, ui.VGroup, ...
        if name.startswith("_"):
            raise AttributeError(name)
        return self._widget

    def HGap(self, *args):
        return Element({})


class FakeDispatcher:
    def __init__(self, ui, script=None):
        self.ui = ui
        self.windows = []
        self.steps = 0
        self.script = script or (lambda window: None)

    def AddWindow(self, props, children):
        window = Window(props, children, dict(self.ui.elements))
        self.ui.elements = {}
        self.windows.append(window)
        return window

    def RunLoop(self):
        self.script(self.windows[-1])

    def ExitLoop(self):
        pass

    def StepLoop(self):
        self.steps += 1


class FakeBmd:
    def __init__(self, script=None):
        self.script = script
        self.dispatcher = None

    def UIDispatcher(self, ui):
        self.dispatcher = FakeDispatcher(ui, self.script)
        return self.dispatcher


class FakeFusion:
    def __init__(self):
        self.UIManager = FakeUIManager()


def _request():
    return ReviewRequest(
        speakers=[SpeakerRow("SPEAKER_00", "Maria", 2, "Hey Jordan"), SpeakerRow("SPEAKER_01", "SPEAKER_01", 2, "Good")],
        templates=["Style A  (Master)", "Style B  (Master)"],
        timeline_name="Timeline 1",
        range_label="whole timeline",
    )


def test_uimanager_review_collects_edits():
    def script(window):
        items = window.GetItems()
        items["Name_SPEAKER_01"].Text = " Jordan "
        items["Template_SPEAKER_01"].CurrentIndex = 1
        items["TextPlus"].Checked = True
        items["Subtitles"].Checked = False
        window.On.Ok.Clicked()

    bmd = FakeBmd(script)
    backend = UIManagerBackend(FakeFusion(), bmd)
    result = backend.review(_request())
    assert result == ReviewResult(names={"SPEAKER_00": "Maria", "SPEAKER_01": "Jordan"}, subtitles=False, textplus=True,
                                  template_index={"SPEAKER_00": 0, "SPEAKER_01": 1})
    window = bmd.dispatcher.windows[-1]
    assert window.shown is False  # hidden after the loop
    assert window.GetItems()["Template_SPEAKER_00"].items == ["Style A  (Master)", "Style B  (Master)"]


def test_uimanager_review_cancel_and_nothing_selected():
    def cancel(window):
        window.On.Cancel.Clicked()

    assert UIManagerBackend(FakeFusion(), FakeBmd(cancel)).review(_request()) is None

    def ok_with_nothing(window):
        items = window.GetItems()
        items["Subtitles"].Checked = False
        items["TextPlus"].Checked = False
        window.On.Ok.Clicked()  # ignored: nothing selected
        window.On["BetterCaptionsReview"].Close()  # user then closes the window

    assert UIManagerBackend(FakeFusion(), FakeBmd(ok_with_nothing)).review(_request()) is None


def test_uimanager_review_without_templates_disables_textplus():
    def script(window):
        window.On.Ok.Clicked()

    request = _request()
    request.templates = []
    result = UIManagerBackend(FakeFusion(), FakeBmd(script)).review(request)
    assert result.template_index == {} and result.subtitles is True and result.textplus is False


def _editable_request():
    from better_captions_plugin.speaker_edits import SpeakerEditor
    from better_captions_plugin.ui import speaker_rows

    from tests.resolve.fake_engine import TRANSCRIPT

    editor = SpeakerEditor(TRANSCRIPT)
    request = _request()
    request.speakers = speaker_rows(editor)
    request.editor = editor
    request.note = "Re-applied 1 speaker fix"
    return request


def test_uimanager_fix_speakers_merges_moves_undoes_and_rebuilds_the_review():
    seen = {"reviews": 0}

    def review_window(window):
        items = window.GetItems()
        seen["reviews"] += 1
        if seen["reviews"] == 1:
            assert "FixSpeakers" in items
            items["Name_SPEAKER_00"].Text = "Maria R"  # typed before opening the editor: kept as a rename
            items["Template_SPEAKER_00"].CurrentIndex = 1
            items["TextPlus"].Checked = True
            window.On.FixSpeakers.Clicked()
        else:
            # Rebuilt from the edited transcript: one speaker left, choices carried over.
            assert "Name_SPEAKER_01" not in items
            assert items["Name_SPEAKER_00"].Text == "Maria R"
            assert items["Template_SPEAKER_00"].CurrentIndex == 1 and items["TextPlus"].Checked
            window.On.Ok.Clicked()

    def editor_window(window):
        items = window.GetItems()
        speakers, cues = items["SpeakerTree"], items["CueTree"]
        assert speakers.header.Text == {0: "Speaker", 1: "Name", 2: "Cues"}
        assert [r.Text for r in speakers.rows] == [{0: "SPEAKER_00", 1: "Maria R", 2: "2"}, {0: "SPEAKER_01", 1: "SPEAKER_01", 2: "2"}]
        assert cues.texts() == ["#1", "#2", "#3", "#4"] and cues.rows[0].Text[3] == "Hey Jordan, / how are you?"
        assert items["Undo"].Enabled  # the rename typed in the review window is undoable here

        # Move "Ready for the race?" to a new speaker, then undo it.
        cues.selected = [cues.row("#3")]
        items["MoveTarget"].CurrentIndex = items["MoveTarget"].items.index("New speaker")
        window.On.Move.Clicked()
        assert items["Status"].Text == "Moved 1 cue to new speaker SPEAKER_02."
        assert items["SpeakerTree"].texts() == ["SPEAKER_00", "SPEAKER_01", "SPEAKER_02"]
        window.On.Undo.Clicked()
        assert items["Status"].Text == "Undid split." and items["SpeakerTree"].texts() == ["SPEAKER_00", "SPEAKER_01"]

        # Filter the cue list by speaker.
        items["Filter"].CurrentIndex = items["Filter"].items.index("SPEAKER_01")
        window.On.Filter.CurrentIndexChanged()
        assert items["CueTree"].texts(3) == ["Good, thanks Maria.", "Always."]

        # Select SPEAKER_01 and merge it into Maria.
        row = speakers.row("SPEAKER_01")
        speakers.current = row
        window.On.SpeakerTree.ItemClicked({"item": row})
        assert items["SpeakerName"].Text == "SPEAKER_01"
        assert items["MergeTarget"].items == ["Maria R (SPEAKER_00)"]
        items["MergeTarget"].CurrentIndex = 0
        window.On.Merge.Clicked()
        assert items["Status"].Text == "Merged SPEAKER_01 into Maria R (SPEAKER_00)."
        assert items["SpeakerTree"].texts() == ["SPEAKER_00"]
        assert items["Filter"].items == ["All speakers", "Maria R (SPEAKER_00)"] and items["Filter"].CurrentIndex == 0
        assert len(items["CueTree"].rows) == 4
        window.On.Done.Clicked()

    def script(window):
        {"BetterCaptionsReview": review_window, "BetterCaptionsSpeakers": editor_window}[window.props["ID"]](window)

    bmd = FakeBmd(script)
    request = _editable_request()
    result = UIManagerBackend(FakeFusion(), bmd).review(request)
    assert result == ReviewResult(names={"SPEAKER_00": "Maria R"}, subtitles=True, textplus=True, template_index={"SPEAKER_00": 1})
    assert [w.props["ID"] for w in bmd.dispatcher.windows] == ["BetterCaptionsReview", "BetterCaptionsSpeakers", "BetterCaptionsReview"]
    assert [op["op"] for op in request.editor.ops] == ["rename", "merge"]
    assert not any(w.shown for w in bmd.dispatcher.windows)


def test_uimanager_editor_rename_and_unselected_actions():
    from better_captions_plugin.speaker_edits import SpeakerEditor
    from better_captions_plugin.ui.speaker_editor import EditorController

    from tests.resolve.fake_engine import TRANSCRIPT

    def script(window):
        items = window.GetItems()
        window.On.Merge.Clicked()  # no speaker picked yet
        assert "Pick a speaker" in items["Status"].Text
        window.On.Move.Clicked()  # no cue selected
        assert "Select one or more cues" in items["Status"].Text
        row = items["SpeakerTree"].row("SPEAKER_01")
        items["SpeakerTree"].current = row
        window.On.SpeakerTree.ItemClicked({})  # falls back to the tree's current row
        items["SpeakerName"].Text = "Jordan"
        window.On.Rename.Clicked()
        assert items["SpeakerTree"].row("SPEAKER_01").Text[1] == "Jordan"
        window.On.BetterCaptionsSpeakers.Close()

    editor = SpeakerEditor(TRANSCRIPT)
    UIManagerBackend(FakeFusion(), FakeBmd(script)).edit_speakers(EditorController(editor))
    assert editor.display_name("SPEAKER_01") == "Jordan"


def test_uimanager_review_without_editor_has_no_fix_button():
    seen = {}

    def script(window):
        seen["items"] = window.GetItems()
        window.On.Ok.Clicked()

    UIManagerBackend(FakeFusion(), FakeBmd(script)).review(_request())
    assert "FixSpeakers" not in seen["items"]


def test_uimanager_progress_and_message():
    bmd = FakeBmd(lambda window: window.On.Ok.Clicked())
    backend = UIManagerBackend(FakeFusion(), bmd)
    view = backend.progress("Better Captions")
    view.set_status("Transcribing…")
    view.set_progress(42.4)
    view.log("line 1")
    view.log("line 2")
    items = bmd.dispatcher.windows[0].GetItems()
    assert items["Status"].Text == "Transcribing…" and items["Percent"].Text == "42%"
    assert items["Log"].PlainText == "line 1\nline 2"
    assert bmd.dispatcher.steps >= 4
    assert view.cancelled is False
    bmd.dispatcher.windows[0].On.Cancel.Clicked()
    assert view.cancelled is True
    view.close()
    backend.message("Title", "body")
    assert bmd.dispatcher.windows[-1].GetItems()["Body"].PlainText == "body"


def test_uimanager_backend_requires_fusion_objects():
    with pytest.raises(RuntimeError):
        UIManagerBackend(None, None)


def stub_tk_module(monkeypatch, stub_cls):
    """Stand in for ui.tk_ui so backend selection is testable without tkinter installed."""
    import types

    import better_captions_plugin.ui as ui_pkg

    mod = types.ModuleType("better_captions_plugin.ui.tk_ui")
    mod.TkBackend = stub_cls
    monkeypatch.setitem(sys.modules, mod.__name__, mod)
    monkeypatch.setattr(ui_pkg, "tk_ui", mod, raising=False)


def test_choose_backend_prefers_uimanager_on_studio_only(monkeypatch):
    import better_captions_plugin.ui as ui_pkg

    created = []

    class StubTk:
        name = "tk"

        def __init__(self):
            created.append("tk")

    class StubUIM:
        name = "uimanager"

        def __init__(self, fusion, bmd):
            created.append("uimanager")

    import better_captions_plugin.ui.uimanager_ui as uim

    stub_tk_module(monkeypatch, StubTk)
    monkeypatch.setattr(uim, "UIManagerBackend", StubUIM)
    fusion = FakeFusion()
    assert ui_pkg.choose_backend(make_world(product="DaVinci Resolve Studio"), fusion, FakeBmd()).name == "uimanager"
    assert ui_pkg.choose_backend(make_world(product="DaVinci Resolve"), fusion, FakeBmd()).name == "tk"
    assert ui_pkg.choose_backend(make_world(product="DaVinci Resolve"), fusion, FakeBmd(), preference="uimanager").name == "uimanager"
    assert ui_pkg.choose_backend(make_world(product="DaVinci Resolve Studio"), fusion, FakeBmd(), preference="tk").name == "tk"
    assert ui_pkg.choose_backend(None, None, None).name == "tk"


def test_choose_backend_falls_back_to_tk_when_uimanager_fails(monkeypatch):
    import better_captions_plugin.ui as ui_pkg

    class StubTk:
        name = "tk"

    stub_tk_module(monkeypatch, StubTk)
    broken_fusion = type("F", (), {"UIManager": None})()
    assert ui_pkg.choose_backend(make_world(product="DaVinci Resolve Studio"), broken_fusion, FakeBmd()).name == "tk"
    with pytest.raises(RuntimeError):
        ui_pkg.choose_backend(None, broken_fusion, FakeBmd(), preference="uimanager")


# --- tkinter (needs a display)


@pytest.fixture
def tk_root():
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip("no display for tkinter: %s" % exc)
    root.withdraw()
    yield root
    try:
        root.destroy()
    except tk.TclError:
        pass


def test_tk_review_dialog_round_trip(tk_root):
    from better_captions_plugin.ui.tk_ui import TkBackend, _ReviewDialog

    backend = TkBackend(root=tk_root)
    request = _request()
    dialog = _ReviewDialog(tk_root, request)
    dialog.name_vars["SPEAKER_01"].set("Jordan")
    dialog.template_boxes["SPEAKER_01"].current(1)
    dialog.textplus_var.set(True)
    dialog._ok()
    assert dialog.result == ReviewResult(names={"SPEAKER_00": "Maria", "SPEAKER_01": "Jordan"}, subtitles=True, textplus=True,
                                         template_index={"SPEAKER_00": 0, "SPEAKER_01": 1})
    view = backend.progress("t")
    view.set_status("x")
    view.set_progress(10)
    view.set_progress(None)
    view.log("hello")
    assert not view.cancelled
    view._cancel()
    assert view.cancelled
    view.close()


def test_tk_fix_speakers_edits_and_rebuilds_the_review(tk_root, monkeypatch):
    from better_captions_plugin.ui import tk_ui

    def scripted_run(self):  # instead of a modal wait: act like the user, then close
        assert self.speaker_tree.get_children() == ("SPEAKER_00", "SPEAKER_01")
        assert self.speaker_tree.item("SPEAKER_00")["values"][1] == "Maria R"
        assert self.cue_tree.get_children() == ("#1", "#2", "#3", "#4")
        self.cue_tree.selection_set("#3")
        self.move_box.current(list(self.move_box["values"]).index("New speaker"))
        self._move()
        assert self.status_var.get() == "Moved 1 cue to new speaker SPEAKER_02."
        assert self.speaker_tree.get_children() == ("SPEAKER_00", "SPEAKER_01", "SPEAKER_02")
        self._undo()
        assert self.speaker_tree.get_children() == ("SPEAKER_00", "SPEAKER_01")
        self.filter_box.current(list(self.filter_box["values"]).index("SPEAKER_01"))
        self._refresh_cues()
        assert self.cue_tree.get_children() == ("#2", "#4")
        self.speaker_tree.selection_set("SPEAKER_01")
        self._on_speaker_selected()
        assert self.name_var.get() == "SPEAKER_01" and list(self.merge_box["values"]) == ["Maria R (SPEAKER_00)"]
        self._merge()
        assert self.speaker_tree.get_children() == ("SPEAKER_00",) and self.filter_box.current() == 0
        assert len(self.cue_tree.get_children()) == 4
        self._close()

    monkeypatch.setattr(tk_ui._SpeakerEditorDialog, "run", scripted_run)
    dialog = tk_ui._ReviewDialog(tk_root, _editable_request())
    dialog.name_vars["SPEAKER_00"].set("Maria R")
    dialog.template_boxes["SPEAKER_00"].current(1)
    dialog._fix_speakers()
    assert list(dialog.name_vars) == ["SPEAKER_00"] and dialog.name_vars["SPEAKER_00"].get() == "Maria R"
    assert dialog.template_boxes["SPEAKER_00"].current() == 1
    dialog._ok()
    assert dialog.result == ReviewResult(names={"SPEAKER_00": "Maria R"}, subtitles=True, textplus=False, template_index={"SPEAKER_00": 1})
