"""Speaker fixes: the edit model, its undo stack, edits.json, planning and the app flow."""

import copy
import json
import os
import sys

import pytest

from better_captions_plugin import app, planning, speaker_edits, transcript as transcript_mod
from better_captions_plugin.speaker_edits import EditError, SpeakerEditor
from better_captions_plugin.timing import TimelineClock
from better_captions_plugin.ui.speaker_editor import NEW_SPEAKER, EditorController, cue_index, cue_number

from tests.resolve.fake_engine import TRANSCRIPT
from tests.resolve.fake_resolve import make_world, parse_srt
from tests.resolve.fake_ui import FakeBackend, accept_all, edit_then_accept

FAKE_ENGINE = os.path.join(os.path.dirname(__file__), "fake_engine.py")


def _word(text, start, end, speaker):
    return {"text": text, "start": start, "end": end, "probability": 1.0, "speaker": speaker}


def sample():
    """Three speakers, with segments and words (the engine's full shape)."""
    return {
        "language": "en",
        "duration": 12.0,
        "speakers": [
            {"id": "SPEAKER_00", "name": "Maria", "name_confidence": 0.8, "name_source": "heuristic"},
            {"id": "SPEAKER_01", "name": "Jordan", "name_confidence": 0.5, "name_source": "heuristic"},
            {"id": "SPEAKER_02", "name": None, "name_confidence": 0.0, "name_source": None},
        ],
        "segments": [
            {
                "speaker": "SPEAKER_00",
                "start": 0.5,
                "end": 2.0,
                "text": "Hey Jordan, how are you?",
                "words": [_word("Hey", 0.5, 0.8, "SPEAKER_00")],
            },
            {
                "speaker": "SPEAKER_01",
                "start": 2.1,
                "end": 3.6,
                "text": "Good, thanks Maria.",
                "words": [_word("Good,", 2.1, 2.5, "SPEAKER_01")],
            },
            {
                "speaker": "SPEAKER_00",
                "start": 4.0,
                "end": 6.5,
                "text": "Ready for the race?",
                "words": [_word("Ready", 4.0, 4.3, "SPEAKER_00")],
            },
            {
                "speaker": "SPEAKER_02",
                "start": 6.4,
                "end": 8.0,
                "text": "Always.",
                "words": [_word("Always.", 6.4, 7.0, "SPEAKER_02")],
            },
        ],
        "cues": [
            {"speaker": "SPEAKER_00", "start": 0.5, "end": 2.0, "lines": ["Hey Jordan,", "how are you?"]},
            {"speaker": "SPEAKER_01", "start": 2.1, "end": 3.6, "lines": ["Good, thanks Maria."]},
            {"speaker": "SPEAKER_00", "start": 4.0, "end": 6.5, "lines": ["Ready for the race?"]},
            {"speaker": "SPEAKER_02", "start": 6.4, "end": 8.0, "lines": ["Always."]},
        ],
        "meta": {},
        "schema_version": 1,
    }


def _cue_speakers(editor):
    return [c["speaker"] for c in editor.data["cues"]]


def _segment_speakers(editor):
    return [(s["speaker"], [w["speaker"] for w in s["words"]]) for s in editor.data["segments"]]


# --- operations


def test_merge_relabels_cues_segments_and_words_and_keeps_the_target_name():
    editor = SpeakerEditor(sample())
    editor.merge("SPEAKER_01", "SPEAKER_00")  # Jordan was really Maria
    assert _cue_speakers(editor) == ["SPEAKER_00", "SPEAKER_00", "SPEAKER_00", "SPEAKER_02"]
    assert _segment_speakers(editor) == [("SPEAKER_00", ["SPEAKER_00"])] * 3 + [("SPEAKER_02", ["SPEAKER_02"])]
    assert [s["id"] for s in editor.data["speakers"]] == ["SPEAKER_00", "SPEAKER_02"]
    assert [(s.id, s.name, s.cue_count) for s in editor.speakers()] == [
        ("SPEAKER_00", "Maria", 3),
        ("SPEAKER_02", "SPEAKER_02", 1),
    ]
    # Merging a named speaker into an unnamed one keeps the target unnamed: names come from A.
    editor.merge("SPEAKER_00", "SPEAKER_02")
    assert [(s.id, s.name, s.cue_count) for s in editor.speakers()] == [("SPEAKER_02", "SPEAKER_02", 4)]


def test_reassign_moves_one_cue_and_only_its_segment():
    editor = SpeakerEditor(sample())
    editor.reassign([2], "SPEAKER_01")  # "Ready for the race?" was Jordan
    assert _cue_speakers(editor) == ["SPEAKER_00", "SPEAKER_01", "SPEAKER_01", "SPEAKER_02"]
    assert _segment_speakers(editor)[0] == ("SPEAKER_00", ["SPEAKER_00"])
    assert _segment_speakers(editor)[2] == ("SPEAKER_01", ["SPEAKER_01"])
    assert [c.text for c in editor.cues("SPEAKER_01")] == ["Good, thanks Maria.", "Ready for the race?"]
    # Moving a cue to the speaker it already has changes nothing but is still one step.
    editor.reassign([0], "SPEAKER_00")
    assert _cue_speakers(editor)[0] == "SPEAKER_00" and len(editor.ops) == 2


def test_split_off_a_new_speaker_from_selected_cues():
    editor = SpeakerEditor(sample())
    new_id = editor.split([1, 3], name="  Sam ")
    assert new_id == "SPEAKER_03"
    assert _cue_speakers(editor) == ["SPEAKER_00", "SPEAKER_03", "SPEAKER_00", "SPEAKER_03"]
    assert editor.data["speakers"][-1] == {
        "id": "SPEAKER_03",
        "name": "Sam",
        "name_confidence": 1.0,
        "name_source": "user",
    }
    assert _segment_speakers(editor)[1] == ("SPEAKER_03", ["SPEAKER_03"])
    # SPEAKER_01 and SPEAKER_02 lost all their cues and drop out of the speaker list shown.
    assert [(s.id, s.name, s.cue_count) for s in editor.speakers()] == [
        ("SPEAKER_00", "Maria", 2),
        ("SPEAKER_03", "Sam", 2),
    ]
    assert editor.split([0]) == "SPEAKER_04" and editor.display_name("SPEAKER_04") == "SPEAKER_04"


def test_rename_marks_the_name_as_the_users():
    editor = SpeakerEditor(sample())
    editor.rename("SPEAKER_02", " Alex ")
    entry = editor.data["speakers"][2]
    assert (entry["name"], entry["name_source"], entry["name_confidence"]) == ("Alex", "user", 1.0)
    assert editor.speakers()[2].source == "user"
    assert not editor.relabelled  # a rename does not change who says what


def test_every_edit_is_undoable_in_order():
    original = sample()
    editor = SpeakerEditor(original)
    snapshots = [copy.deepcopy(editor.data)]
    editor.rename("SPEAKER_02", "Alex")
    snapshots.append(copy.deepcopy(editor.data))
    editor.merge("SPEAKER_01", "SPEAKER_00")
    snapshots.append(copy.deepcopy(editor.data))
    editor.reassign([3], "SPEAKER_00")
    snapshots.append(copy.deepcopy(editor.data))
    editor.split([0, 2])
    assert editor.relabelled and editor.can_undo
    for kind, expected in zip(["split", "reassign", "merge", "rename"], reversed(snapshots)):
        assert editor.undo()["op"] == kind
        assert editor.data == expected
    assert editor.undo() is None and not editor.can_undo
    assert editor.data == original == editor.base  # the original transcript was never touched


@pytest.mark.parametrize(
    "action",
    [
        lambda e: e.merge("SPEAKER_00", "SPEAKER_00"),
        lambda e: e.merge("SPEAKER_09", "SPEAKER_00"),
        lambda e: e.reassign([0], "SPEAKER_09"),
        lambda e: e.reassign([7], "SPEAKER_01"),
        lambda e: e.split([]),
        lambda e: e.rename("SPEAKER_09", "x"),
    ],
)
def test_invalid_edits_raise_and_change_nothing(action):
    editor = SpeakerEditor(sample())
    with pytest.raises(EditError):
        action(editor)
    assert editor.data == sample() and editor.ops == []


def test_apply_op_rejects_unknown_and_malformed_edits():
    data = sample()
    with pytest.raises(EditError):
        speaker_edits.apply_op(data, {"op": "explode"})
    with pytest.raises(EditError):
        speaker_edits.apply_op(data, {"op": "merge", "source": "SPEAKER_01"})
    assert data == sample()


def test_record_names_adds_renames_only_for_changed_names():
    editor = SpeakerEditor(sample())
    pushed = editor.record_names(
        {"SPEAKER_00": "Maria", "SPEAKER_01": " Jordan  B ", "SPEAKER_02": "", "SPEAKER_09": "Ghost"}
    )
    assert pushed == [{"op": "rename", "speaker": "SPEAKER_01", "name": "Jordan B"}]
    assert editor.display_name("SPEAKER_01") == "Jordan B"


# --- edits.json and re-applying on a re-run


def test_saved_edits_reapply_to_a_rerun_by_cue_time(tmp_path):
    editor = SpeakerEditor(sample())
    editor.merge("SPEAKER_02", "SPEAKER_01")
    editor.reassign([2], "SPEAKER_01")
    new_id = editor.split([0], name="Sam")
    path = str(tmp_path / "out" / speaker_edits.EDITS_FILE)
    speaker_edits.save_edits(path, editor, audio_sha256="abc")
    saved = speaker_edits.load_edits(path)
    assert saved["version"] == speaker_edits.EDITS_VERSION and saved["audio_sha256"] == "abc"
    assert [op["op"] for op in saved["ops"]] == ["merge", "reassign", "split"]

    # The re-run found one more cue at the start, so every index shifted by one; times still match.
    rerun = sample()
    rerun["cues"].insert(0, {"speaker": "SPEAKER_01", "start": 0.0, "end": 0.4, "lines": ["Hm."]})
    again = SpeakerEditor(rerun)
    assert again.reapply(saved["ops"]) == 3 and again.skipped == []
    assert [c["speaker"] for c in again.data["cues"]] == [
        "SPEAKER_01",
        new_id,
        "SPEAKER_01",
        "SPEAKER_01",
        "SPEAKER_01",
    ]
    assert again.display_name(new_id) == "Sam"
    # Re-applied edits are on the stack like any other.
    assert again.undo()["op"] == "split"


def test_saved_edits_that_no_longer_fit_are_skipped():
    editor = SpeakerEditor(sample())
    editor.reassign([3], "SPEAKER_00")
    ops = copy.deepcopy(editor.ops)
    rerun = sample()
    rerun["cues"][3]["start"] = 7.0  # the cue moved: no longer the same cue
    rerun["speakers"] = rerun["speakers"][:2]
    again = SpeakerEditor(rerun)
    assert again.reapply(ops + [{"op": "merge", "source": "SPEAKER_05", "target": "SPEAKER_00"}, "junk"]) == 0
    assert len(again.skipped) == 3 and again.ops == []
    assert again.data == rerun


def test_find_saved_edits_matches_the_audio(tmp_path):
    editor = SpeakerEditor(sample())
    editor.merge("SPEAKER_01", "SPEAKER_00")
    first, second = str(tmp_path / "a.json"), str(tmp_path / "b.json")
    speaker_edits.save_edits(first, editor, audio_sha256="other-audio")
    speaker_edits.save_edits(second, editor, audio_sha256="this-audio")
    assert (
        speaker_edits.find_saved_edits([None, str(tmp_path / "missing.json"), first, second], "this-audio")[0] == second
    )
    assert speaker_edits.find_saved_edits([first], "this-audio") == (None, None)
    (tmp_path / "garbage.json").write_text("{nope")
    (tmp_path / "newer.json").write_text(json.dumps({"version": 99, "ops": []}))
    assert speaker_edits.find_saved_edits([str(tmp_path / "garbage.json"), str(tmp_path / "newer.json")]) == (
        None,
        None,
    )
    assert speaker_edits.audio_fingerprint(str(tmp_path / "missing.wav")) is None


def test_speaker_srts_are_rewritten_from_the_edited_transcript(tmp_path):
    editor = SpeakerEditor(sample())
    editor.merge("SPEAKER_02", "SPEAKER_01")
    paths = transcript_mod.write_speaker_srts(editor.transcript(), str(tmp_path))
    assert sorted(paths) == ["SPEAKER_00", "SPEAKER_01"]
    assert parse_srt(paths["SPEAKER_01"]) == [(2.1, 3.6, "Good, thanks Maria."), (6.4, 8.0, "Always.")]
    assert parse_srt(paths["SPEAKER_00"])[0] == (0.5, 2.0, "Hey Jordan,\nhow are you?")


# --- planning uses the edited transcript


def test_planning_after_merge_puts_every_cue_on_one_track():
    editor = SpeakerEditor(TRANSCRIPT)
    editor.merge("SPEAKER_01", "SPEAKER_00")
    clock = TimelineClock(fps=24, start_frame=86400)
    plans = planning.plan_tracks(editor.transcript(), {}, planning.MODE_TEXTPLUS, clock)
    assert [(p.speaker_id, p.name) for p in plans] == [("SPEAKER_00", "Maria")]
    assert [c.text for c in plans[0].clips] == [
        "Hey Jordan,\nhow are you?",
        "Good, thanks Maria.",
        "Ready for the race?",
        "Always.",
    ]
    assert [c.record_frame for c in plans[0].clips] == [86412, 86450, 86496, 86554]
    for a, b in zip(plans[0].clips, plans[0].clips[1:]):
        assert a.end_frame <= b.record_frame  # 4.0-6.5 and 6.4-8.0 overlapped once on one track


def test_planning_after_reassign_and_split():
    editor = SpeakerEditor(TRANSCRIPT)
    editor.reassign([3], "SPEAKER_00")
    new_id = editor.split([1], name="Sam")
    plans = planning.plan_tracks(
        editor.transcript(), {"SPEAKER_00": "Maria"}, planning.MODE_SUBTITLES, TimelineClock(fps=24)
    )
    assert [(p.speaker_id, p.name, [c.text for c in p.clips]) for p in plans] == [
        ("SPEAKER_00", "Maria", ["Hey Jordan,\nhow are you?", "Ready for the race?", "Always."]),
        (new_id, "Sam", ["Good, thanks Maria."]),
    ]


# --- the dialog's controller (shared by both UI backends)


def test_editor_controller_lists_filters_moves_merges_and_undoes():
    controller = EditorController(SpeakerEditor(TRANSCRIPT))
    assert controller.speaker_rows() == [("SPEAKER_00", "Maria", "2"), ("SPEAKER_01", "SPEAKER_01", "2")]
    assert controller.filter_choices() == [
        ("All speakers", None),
        ("Maria (SPEAKER_00)", "SPEAKER_00"),
        ("SPEAKER_01", "SPEAKER_01"),
    ]
    assert controller.cue_rows("SPEAKER_01") == [
        ("#2", "0:02.1", "SPEAKER_01", "Good, thanks Maria."),
        ("#4", "0:06.4", "SPEAKER_01", "Always."),
    ]
    assert controller.cue_rows()[0] == ("#1", "0:00.5", "Maria", "Hey Jordan, / how are you?")
    assert controller.merge_choices("SPEAKER_00") == [("SPEAKER_01", "SPEAKER_01")]
    assert controller.move_choices()[-1] == ("New speaker", NEW_SPEAKER)

    assert (
        controller.move([cue_index("#4")], NEW_SPEAKER)
        and controller.status == "Moved 1 cue to new speaker SPEAKER_02."
    )
    assert controller.rename("SPEAKER_02", "Sam") and controller.cue_rows("SPEAKER_02")[0][2] == "Sam"
    assert controller.move([0, 2], "SPEAKER_01") and controller.status == "Moved 2 cues to SPEAKER_01."
    assert controller.merge("SPEAKER_02", "SPEAKER_01") and controller.speaker_rows() == [
        ("SPEAKER_01", "SPEAKER_01", "4")
    ]
    assert controller.undo() and controller.status == "Undid merge."
    assert [row[0] for row in controller.speaker_rows()] == ["SPEAKER_01", "SPEAKER_02"]

    assert not controller.move([], "SPEAKER_01") and "Select" in controller.status
    assert not controller.merge("SPEAKER_01", None)
    assert not controller.merge("SPEAKER_01", "SPEAKER_01") and "itself" in controller.status
    assert not controller.rename("SPEAKER_01", "  ")
    while controller.can_undo:
        controller.undo()
    assert not controller.undo() and controller.status == "Nothing to undo."
    assert cue_number(0) == "#1" and cue_index("#12") == 11 and cue_index("x") is None and cue_index("#0") is None


def test_editor_controller_uses_the_timeline_time_label():
    clock = TimelineClock(fps=24, start_frame=86400)
    controller = EditorController(SpeakerEditor(TRANSCRIPT), time_label=lambda s: clock.timecode(clock.frame_at(s)))
    assert controller.cue_rows()[0][1] == "01:00:00:12"


# --- the app flow with scripted edits in the review dialog


@pytest.fixture
def config(tmp_path):
    return {
        "engine": [sys.executable, "-I", FAKE_ENGINE],
        "model": "fake-model",
        "keep_work_dir": True,
        "_path": str(tmp_path / "data" / "config.json"),  # edits are kept in <data>/edits
    }


def _run(resolve, backend, config, tmp_path, name):
    return app.main(
        resolve=resolve, backend=backend, config=config, sleep=lambda s: None, work_dir=str(tmp_path / name)
    )


def _frames(timeline, cues):
    return [(timeline.start_frame + int(round(c["start"] * timeline.fps)), "\n".join(c["lines"])) for c in cues]


def test_app_merge_in_review_writes_one_track_and_saves_edits(config, tmp_path):
    resolve = make_world(frame_rate="24", start_timecode="01:00:00:00")
    backend = FakeBackend(edit_then_accept(lambda editor: editor.merge("SPEAKER_01", "SPEAKER_00")))
    outcome = _run(resolve, backend, config, tmp_path, "run1")
    assert outcome.error is None and not outcome.subtitle_fallbacks, (backend.errors, outcome.subtitle_fallbacks)
    request = backend.requests[0]
    assert request.editor is not None and request.note == ""
    assert request.time_label(0.5) == "01:00:00:12"
    tl = resolve.project.timeline
    # One subtitle track with all four cues: the SRT was rewritten from the edited transcript.
    assert [t.name for t in tl.tracks["subtitle"]] == ["Maria"]
    assert [(i.GetStart(), i.GetName()) for i in tl.track("subtitle", "Maria").items] == _frames(tl, TRANSCRIPT["cues"])
    # The edits sit next to the run's transcript.json and in the per-audio store.
    out_dir = os.path.join(outcome.work_dir, "out")
    saved = json.loads(open(os.path.join(out_dir, "edits.json"), encoding="utf-8").read())
    assert saved["ops"] == [{"op": "merge", "source": "SPEAKER_01", "target": "SPEAKER_00"}]
    stored = os.listdir(str(tmp_path / "data" / "edits"))
    assert stored == [saved["audio_sha256"] + ".json"]
    assert os.path.isfile(os.path.join(out_dir, "transcript.json"))


def test_app_rerun_on_the_same_audio_reapplies_saved_edits_and_undo_forgets_them(config, tmp_path):
    first = FakeBackend(
        edit_then_accept(lambda editor: editor.merge("SPEAKER_01", "SPEAKER_00"), names={"SPEAKER_00": "Maria R"})
    )
    _run(make_world(), first, config, tmp_path, "run1")

    # Re-run in a fresh work dir: the store finds the edits by the rendered audio.
    second = FakeBackend(accept_all())
    resolve = make_world()
    outcome = _run(resolve, second, config, tmp_path, "run2")
    request = second.requests[0]
    assert [(r.id, r.name, r.cue_count) for r in request.speakers] == [("SPEAKER_00", "Maria R", 4)]
    assert "Re-applied 2 speaker fixes" in request.note
    assert [t.name for t in resolve.project.timeline.tracks["subtitle"]] == ["Maria R"]
    assert (
        json.load(open(os.path.join(outcome.work_dir, "out", "edits.json"), encoding="utf-8"))["ops"][0]["op"]
        == "merge"
    )

    # Third run: the user undoes everything; the saved fixes are forgotten.
    def undo_all(editor):
        while editor.undo():
            pass

    third = FakeBackend(edit_then_accept(undo_all))
    resolve = make_world()
    outcome = _run(resolve, third, config, tmp_path, "run3")
    assert [t.name for t in resolve.project.timeline.tracks["subtitle"]] == ["Maria", "SPEAKER_01"]
    assert os.listdir(str(tmp_path / "data" / "edits")) == []
    assert not os.path.exists(os.path.join(outcome.work_dir, "out", "edits.json"))


def test_app_split_and_reassign_before_textplus_tracks(config, tmp_path):
    def edit(editor):
        editor.reassign([3], "SPEAKER_00")  # "Always." was Maria
        editor.split([2], name="Sam")  # "Ready for the race?" was a third person

    backend = FakeBackend(edit_then_accept(edit, subtitles=False, textplus=True))
    resolve = make_world()
    outcome = _run(resolve, backend, config, tmp_path, "run1")
    assert outcome.error is None, backend.errors
    tl = resolve.project.timeline
    assert [t.name for t in tl.tracks["video"]] == ["Video 1", "Maria", "SPEAKER_01", "Sam"]
    assert [i.text() for i in tl.track("video", "Maria").items] == ["Hey Jordan,\nhow are you?", "Always."]
    assert [i.text() for i in tl.track("video", "SPEAKER_01").items] == ["Good, thanks Maria."]
    assert [i.text() for i in tl.track("video", "Sam").items] == ["Ready for the race?"]


def test_app_cancelled_review_saves_no_edits(config, tmp_path):
    def cancel(request):
        request.editor.merge("SPEAKER_01", "SPEAKER_00")
        return None

    outcome = _run(make_world(), FakeBackend(cancel), config, tmp_path, "run1")
    assert outcome.cancelled
    assert not os.path.exists(os.path.join(outcome.work_dir, "out", "edits.json"))
    assert not os.path.isdir(str(tmp_path / "data" / "edits")) or os.listdir(str(tmp_path / "data" / "edits")) == []
