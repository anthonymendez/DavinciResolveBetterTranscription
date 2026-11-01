"""Per-cue speaker confidence: uncertain lines are counted, marked and listed first for checking."""

import json
import os
import sys

import pytest

from better_captions_plugin import app
from better_captions_plugin import transcript as transcript_mod
from better_captions_plugin.speaker_edits import SpeakerEditor, edits_document
from better_captions_plugin.timing import TimelineClock
from better_captions_plugin.ui import (
    SpeakerRow,
    checking_label,
    fix_speakers_label,
    review_check_note,
    review_speaker_label,
    speaker_rows,
)
from better_captions_plugin.ui.speaker_editor import NEW_SPEAKER, EditorController
from better_captions_plugin.ui.uimanager_ui import UIManagerBackend
from tests.resolve.fake_engine import TRANSCRIPT, with_confidence
from tests.resolve.fake_resolve import make_world
from tests.resolve.fake_ui import FakeBackend, accept_all, edit_then_accept
from tests.resolve.test_ui import FakeBmd, FakeFusion, _request, tk_root  # noqa: F401  (tk_root: fixture)

FAKE_ENGINE = os.path.join(os.path.dirname(__file__), "fake_engine.py")


def confident():
    """The fake engine's transcript with confidences: cues #2 and #4 uncertain, #3 unknown."""
    return with_confidence(TRANSCRIPT)


# --- transcript.json


@pytest.mark.parametrize(
    "value, expected",
    [
        (0.42, 0.42),
        (0, 0.0),
        (1, 1.0),
        ("0.5", 0.5),
        (None, None),
        ("high", None),
        (True, None),
        (1.5, None),
        (-0.1, None),
        (float("nan"), None),
        ([0.3], None),
    ],
)
def test_cue_confidence_is_parsed_tolerantly(value, expected):
    data = json.loads(json.dumps(TRANSCRIPT))
    data["cues"][0]["speaker_confidence"] = value
    cue = transcript_mod.from_dict(data).cues[0]
    assert cue.speaker_confidence == expected


def test_missing_confidence_is_unknown_and_not_uncertain():
    transcript = transcript_mod.from_dict(TRANSCRIPT)
    assert all(c.speaker_confidence is None and not c.uncertain for c in transcript.cues)
    assert int(TRANSCRIPT["schema_version"]) == transcript_mod.SUPPORTED_SCHEMA_VERSION == 1


def test_is_uncertain_threshold_and_inputs():
    below = transcript_mod.UNCERTAIN_BELOW - 0.01
    assert transcript_mod.UNCERTAIN_BELOW == 0.6
    assert transcript_mod.is_uncertain({"speaker_confidence": below})
    assert not transcript_mod.is_uncertain({"speaker_confidence": transcript_mod.UNCERTAIN_BELOW})
    assert not transcript_mod.is_uncertain({"speaker_confidence": None}) and not transcript_mod.is_uncertain({})
    assert not transcript_mod.is_uncertain({"speaker_confidence": "bogus"})
    cues = transcript_mod.from_dict(confident()).cues
    assert [c.speaker_confidence for c in cues] == [0.95, 0.42, None, 0.31]
    assert [c.uncertain for c in cues] == [False, True, False, True]


# --- the edit model


def test_editor_carries_confidence_and_counts_uncertain_cues():
    editor = SpeakerEditor(confident())
    assert [(c.index, c.speaker_confidence, c.uncertain) for c in editor.cues()] == [
        (0, 0.95, False),
        (1, 0.42, True),
        (2, None, False),
        (3, 0.31, True),
    ]
    assert editor.uncertain_count() == 2
    assert [(s.id, s.uncertain_count) for s in editor.speakers()] == [("SPEAKER_00", 0), ("SPEAKER_01", 2)]
    assert [r.uncertain for r in speaker_rows(editor)] == [0, 2]
    # The transcript the tracks are made from keeps the confidences.
    assert [c.speaker_confidence for c in editor.transcript().cues] == [0.95, 0.42, None, 0.31]


def test_moving_or_confirming_a_cue_checks_it_off_and_undo_brings_it_back():
    editor = SpeakerEditor(confident())
    editor.reassign([1], "SPEAKER_00")
    assert editor.uncertain_count() == 1 and editor.data["cues"][1]["speaker_confidence"] == 1.0
    editor.split([3])
    assert editor.uncertain_count() == 0 and editor.data["cues"][3]["speaker_confidence"] == 1.0
    editor.undo()
    editor.undo()
    assert editor.uncertain_count() == 2 and editor.data["cues"][1]["speaker_confidence"] == 0.42
    # "Moving" a cue to the speaker it already has, or confirming it, says that speaker is right.
    editor.reassign([1], "SPEAKER_01")
    assert editor.data["cues"][1]["speaker"] == "SPEAKER_01" and editor.uncertain_count() == 1
    op = editor.confirm([3])
    assert op == {"op": "confirm", "cues": [{"index": 3, "start": 6.4, "end": 8.0}]}
    assert editor.uncertain_count() == 0
    # Confirming relabels nothing, so the engine's per-speaker SRTs stay valid.
    only_confirmed = SpeakerEditor(confident())
    only_confirmed.confirm([1, 3])
    assert only_confirmed.uncertain_count() == 0 and not only_confirmed.relabelled
    # The original transcript is untouched.
    assert editor.base["cues"][3]["speaker_confidence"] == 0.31


def test_merge_does_not_check_individual_lines():
    editor = SpeakerEditor(confident())
    editor.merge("SPEAKER_01", "SPEAKER_00")
    assert editor.uncertain_count() == 2


def test_saved_edits_keep_lines_checked_when_reapplied():
    first = SpeakerEditor(confident())
    first.reassign([1], "SPEAKER_00")
    first.confirm([3])
    saved = json.loads(json.dumps(edits_document(first)))
    again = SpeakerEditor(confident())
    assert again.reapply(saved["ops"]) == 2 and not again.skipped
    assert again.uncertain_count() == 0
    assert [c.speaker_confidence for c in again.cues()] == [0.95, 1.0, None, 1.0]


# --- the "Fix speakers" controller


def test_controller_without_confidence_looks_as_before():
    controller = EditorController(SpeakerEditor(TRANSCRIPT))
    assert not controller.checking and not controller.only_uncertain
    assert [key for key, _title, _width in controller.cue_columns()] == ["num", "time", "speaker", "text"]
    assert [row[0] for row in controller.cue_rows()] == ["#1", "#2", "#3", "#4"]
    assert controller.cue_rows()[0] == ("#1", "0:00.5", "Maria", "Hey Jordan, / how are you?")
    assert controller.check_status() == "" and not controller.is_check_row(controller.cue_rows()[0])


def test_controller_with_only_confident_lines_looks_as_before():
    controller = EditorController(SpeakerEditor(with_confidence(TRANSCRIPT, (0.9, 0.8, 0.7, 0.6))))
    assert not controller.checking and len(controller.cue_rows()[0]) == 4 and len(controller.cue_rows()) == 4


def test_controller_lists_uncertain_lines_first_and_marks_them():
    controller = EditorController(SpeakerEditor(confident()))
    assert controller.checking and controller.only_uncertain  # starts on the lines to check
    assert [title for _key, title, _width in controller.cue_columns()] == ["#", "Check", "Time", "Speaker", "Text"]
    assert controller.cue_rows() == [
        ("#2", "?", "0:02.1", "SPEAKER_01", "Good, thanks Maria."),
        ("#4", "?", "0:06.4", "SPEAKER_01", "Always."),
    ]
    assert controller.check_status() == "2 lines need checking"
    assert all(controller.is_check_row(row) for row in controller.cue_rows())
    # All lines: the uncertain ones first, then the rest, each in time order.
    controller.only_uncertain = False
    rows = controller.cue_rows()
    assert [(row[0], row[1]) for row in rows] == [("#2", "?"), ("#4", "?"), ("#1", ""), ("#3", "")]
    assert [controller.is_check_row(row) for row in rows] == [True, True, False, False]
    # The speaker filter still applies.
    assert [row[0] for row in controller.cue_rows("SPEAKER_00")] == ["#1", "#3"]
    controller.only_uncertain = True
    assert controller.cue_rows("SPEAKER_00") == []


def test_controller_checks_lines_off_as_the_user_works():
    controller = EditorController(SpeakerEditor(confident()))
    assert not controller.confirm([]) and controller.status == "Select one or more cues first."
    assert controller.confirm([1]) and controller.status == "Marked 1 cue as checked."
    assert [row[0] for row in controller.cue_rows()] == ["#4"]
    assert controller.check_status() == "1 line needs checking"
    assert controller.move([3], NEW_SPEAKER) and controller.cue_rows() == []
    assert controller.check_status() == "All lines checked."
    assert controller.checking  # the columns stay put for the rest of the dialog
    assert controller.undo() and controller.check_status() == "1 line needs checking"


# --- the review dialog's labels


def test_review_labels():
    assert checking_label(0) == "" and checking_label(1) == "1 line needs checking"
    assert checking_label(3) == "3 lines need checking"
    plain = SpeakerRow("SPEAKER_00", "Maria", 2, "Hey")
    doubtful = SpeakerRow("SPEAKER_01", "SPEAKER_01", 5, "Good", uncertain=3)
    assert review_speaker_label(plain) == "SPEAKER_00 (2 cues)"
    assert review_speaker_label(doubtful) == "SPEAKER_01 (5 cues, 3 to check)"
    assert review_check_note([plain], True) == "" and fix_speakers_label([plain]) == "Fix speakers…"
    assert review_check_note([plain, doubtful], True) == (
        "3 lines need checking: the voice match was unsure. Fix speakers… lists them first."
    )
    assert review_check_note([doubtful], False) == "3 lines need checking: the voice match was unsure."
    assert fix_speakers_label([plain, doubtful]) == "Fix speakers… (3 to check)"


def test_review_request_without_editor_counts_uncertain_lines():
    clock = TimelineClock(fps=24, start_frame=86400)
    transcript = transcript_mod.from_dict(confident())
    request = app.build_review_request(transcript, [], "Timeline 1", clock, 86400, None, {})
    assert [(r.id, r.uncertain) for r in request.speakers] == [("SPEAKER_00", 0), ("SPEAKER_01", 2)]


# --- UIManager


def _editable(data):
    editor = SpeakerEditor(data)
    request = _request()
    request.speakers = speaker_rows(editor)
    request.editor = editor
    return request


def test_uimanager_review_and_editor_put_uncertain_lines_first():
    seen = {"reviews": 0}

    def review_window(window):
        items = window.GetItems()
        seen["reviews"] += 1
        if seen["reviews"] == 1:
            assert items["CheckNote"].Text.startswith("2 lines need checking")
            assert items["FixSpeakers"].Text == "Fix speakers… (2 to check)"
            window.On.FixSpeakers.Clicked()
        else:  # every line checked: the review looks as without confidence data
            assert "CheckNote" not in items and items["FixSpeakers"].Text == "Fix speakers…"
            window.On.Ok.Clicked()

    def editor_window(window):
        items = window.GetItems()
        cues = items["CueTree"]
        assert cues.header.Text == {0: "#", 1: "Check", 2: "Time", 3: "Speaker", 4: "Text"}
        assert cues.texts() == ["#2", "#4"] and cues.texts(1) == ["?", "?"]
        assert items["OnlyUncertain"].Checked and items["CheckCount"].Text == "2 lines need checking"
        # Show every line: the doubtful ones stay on top.
        items["OnlyUncertain"].Checked = False
        window.On.OnlyUncertain.Clicked()
        assert items["CueTree"].texts() == ["#2", "#4", "#1", "#3"]
        items["OnlyUncertain"].Checked = True
        window.On.OnlyUncertain.Clicked()
        # "#2" is right; "#4" is really Maria.
        items["CueTree"].selected = [items["CueTree"].row("#2")]
        window.On.Confirm.Clicked()
        assert items["Status"].Text == "Marked 1 cue as checked." and items["CueTree"].texts() == ["#4"]
        items["CueTree"].selected = [items["CueTree"].row("#4")]
        items["MoveTarget"].CurrentIndex = items["MoveTarget"].items.index("Maria (SPEAKER_00)")
        window.On.Move.Clicked()
        assert items["CueTree"].texts() == [] and items["CheckCount"].Text == "All lines checked."
        window.On.Done.Clicked()

    def script(window):
        {"BetterCaptionsReview": review_window, "BetterCaptionsSpeakers": editor_window}[window.props["ID"]](window)

    request = _editable(confident())
    result = UIManagerBackend(FakeFusion(), FakeBmd(script)).review(request)
    assert result is not None and seen["reviews"] == 2
    assert [op["op"] for op in request.editor.ops] == ["confirm", "reassign"]


def test_uimanager_without_confidence_has_no_check_tools():
    seen = {}

    def script(window):
        items = window.GetItems()
        if window.props["ID"] == "BetterCaptionsReview":
            if "editor" in seen:
                window.On.Ok.Clicked()
                return
            assert "CheckNote" not in items and items["FixSpeakers"].Text == "Fix speakers…"
            window.On.FixSpeakers.Clicked()
        else:
            seen["editor"] = items
            assert items["CueTree"].header.Text == {0: "#", 1: "Time", 2: "Speaker", 3: "Text"}
            window.On.Done.Clicked()

    UIManagerBackend(FakeFusion(), FakeBmd(script)).review(_editable(TRANSCRIPT))
    assert "OnlyUncertain" not in seen["editor"] and "Confirm" not in seen["editor"]


# --- tkinter (real windows; BETTER_CAPTIONS_GUI_TESTS=1)


def test_tk_editor_lists_uncertain_lines_first(tk_root, monkeypatch):  # noqa: F811
    from better_captions_plugin.ui import tk_ui

    def scripted_run(self):
        assert self.cue_tree["columns"] == ("num", "check", "time", "speaker", "text")
        assert self.cue_tree.get_children() == ("#2", "#4")
        assert self.cue_tree.item("#2", "tags") == ("check",)
        assert self.check_var.get() == "2 lines need checking"
        self.only_uncertain_var.set(False)
        self._toggle_uncertain()
        assert self.cue_tree.get_children() == ("#2", "#4", "#1", "#3")
        assert self.cue_tree.item("#1", "tags") == ""
        self.cue_tree.selection_set("#2")
        self._confirm()
        assert self.cue_tree.get_children() == ("#4", "#1", "#2", "#3")
        assert self.check_var.get() == "1 line needs checking"
        self._close()

    monkeypatch.setattr(tk_ui._SpeakerEditorDialog, "run", scripted_run)
    dialog = tk_ui._ReviewDialog(tk_root, _editable(confident()))
    assert dialog.check_note.cget("text").startswith("2 lines need checking")
    assert dialog.fix_button.cget("text") == "Fix speakers… (2 to check)"
    dialog._fix_speakers()
    assert dialog.check_note.cget("text").startswith("1 line needs checking")
    assert dialog.fix_button.cget("text") == "Fix speakers… (1 to check)"
    dialog._cancel()


def test_tk_review_without_confidence_has_no_check_note(tk_root):  # noqa: F811
    from better_captions_plugin.ui import tk_ui

    dialog = tk_ui._ReviewDialog(tk_root, _editable(TRANSCRIPT))
    assert dialog.check_note.cget("text") == "" and not dialog.check_note.winfo_manager()
    assert dialog.fix_button.cget("text") == "Fix speakers…"
    dialog._cancel()


# --- the app flow against the fake engine


@pytest.fixture
def config(tmp_path):
    return {
        "engine": [sys.executable, "-I", FAKE_ENGINE],
        "engine_args": ["--with-confidence"],
        "model": "fake-model",
        "keep_work_dir": True,
        "_path": str(tmp_path / "data" / "config.json"),  # edits are kept in <data>/edits
    }


def _run(resolve, backend, config, tmp_path, name):
    return app.main(
        resolve=resolve, backend=backend, config=config, sleep=lambda s: None, work_dir=str(tmp_path / name)
    )


def test_app_flow_flags_uncertain_lines_and_remembers_checked_ones(config, tmp_path):
    def check_lines(editor):
        controller = EditorController(editor)
        assert [row[0] for row in controller.cue_rows()] == ["#2", "#4"]
        controller.confirm([1])
        controller.move([3], "SPEAKER_00")

    first = FakeBackend(edit_then_accept(check_lines))
    outcome = _run(make_world(), first, config, tmp_path, "run1")
    assert outcome.error is None, first.errors
    assert [(r.id, r.uncertain) for r in first.requests[0].speakers] == [("SPEAKER_00", 0), ("SPEAKER_01", 2)]

    # Re-run on the same audio: the saved fixes are re-applied, so nothing is left to check.
    second = FakeBackend(accept_all())
    outcome = _run(make_world(), second, config, tmp_path, "run2")
    assert outcome.error is None, second.errors
    request = second.requests[0]
    assert "Re-applied 2 speaker fixes" in request.note
    assert [r.uncertain for r in request.speakers] == [0, 0]
    assert review_check_note(request.speakers, True) == "" and request.editor.uncertain_count() == 0
