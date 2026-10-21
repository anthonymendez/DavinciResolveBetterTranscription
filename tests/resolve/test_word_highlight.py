"""Highlight words as they're spoken: timing (pure), the comp edit, the Resolve calls, the dialog and config."""

import dataclasses
import json
import os
import random
import sys
from itertools import pairwise

import pytest

from better_captions_plugin import app, planning, resolve_io, word_highlight
from better_captions_plugin import config as config_mod
from better_captions_plugin.timing import TimelineClock
from better_captions_plugin.transcript import Word, from_dict
from better_captions_plugin.ui import ReviewRequest, ReviewResult, SpeakerRow
from better_captions_plugin.ui.uimanager_ui import UIManagerBackend
from better_captions_plugin.word_highlight import (
    CLS_BLUE,
    CLS_GREEN,
    CLS_RED,
    CueWord,
    HighlightStep,
    HighlightUnsupported,
    add_highlight_to_comp,
    cue_words,
    highlight_steps,
)
from tests.resolve.fake_engine import TRANSCRIPT, with_words
from tests.resolve.fake_resolve import (
    FakeComp,
    FakeMediaPoolItem,
    FakeTimelineItem,
    FakeTool,
    make_world,
    parse_imported_comp,
)
from tests.resolve.fake_ui import FakeBackend, accept_all
from tests.resolve.test_ui import FakeBmd, FakeFusion

CLOCK = TimelineClock(fps=24.0, start_frame=0)
FAKE_ENGINE = os.path.join(os.path.dirname(__file__), "fake_engine.py")


def cw(text, start, end, first=0, last=0):
    return CueWord(text, first, last, start, end)


def frames(steps):
    return [(s.record_frame, s.duration, s.word.text if s.word else None) for s in steps]


# --- colour


def test_colours_are_normalised_and_converted_to_fusion_range():
    assert word_highlight.normalize_hex_color("#ffd400") == "#FFD400"
    assert word_highlight.normalize_hex_color(" fd0 ") == "#FFDD00"
    assert word_highlight.parse_hex_color("#FF8000") == (1.0, round(128 / 255, 6), 0.0)
    for bad in ("", None, "#12345", "yellow", "#GGGGGG"):
        with pytest.raises(ValueError):
            word_highlight.normalize_hex_color(bad)


# --- which words make up a cue


def test_cue_words_match_tokens_to_timed_words_with_character_ranges():
    words = [
        Word(" Hey", 0.5, 0.7, "S0"),
        Word(" Jordan,", 0.75, 1.1, "S0"),
        Word(" Hi", 0.8, 0.9, "S1"),
        Word(" how", 1.2, 1.4, "S0"),
        Word(" are", 1.45, 1.6, "S0"),
        Word(" you?", 1.65, 2.0, "S0"),
        Word(" later", 9.0, 9.5, "S0"),
    ]
    text = "Hey Jordan,\nhow are you?"
    result = cue_words(text, 0.5, 2.0, words, speaker="S0")
    assert [(w.text, w.char_start, w.char_end, w.start, w.end) for w in result] == [
        ("Hey", 0, 2, 0.5, 0.7),
        ("Jordan,", 4, 10, 0.75, 1.1),
        ("how", 12, 14, 1.2, 1.4),  # "\n" counts as one
        ("are", 16, 18, 1.45, 1.6),
        ("you?", 20, 23, 1.65, 2.0),
    ]
    for word in result:
        assert text[word.char_start : word.char_end + 1] == word.text


def test_cue_words_prefer_the_cue_speakers_words_and_match_loosely():
    words = [Word(" so", 1.0, 1.1, "S1"), Word(" so", 1.05, 1.2, "S0"), Word(" READY", 1.3, 1.6, "S0")]
    result = cue_words("So, ready?", 1.0, 1.6, words, speaker="S0")
    assert [(w.text, w.start) for w in result] == [("So,", 1.05), ("ready?", 1.3)]
    # Speaker fixes relabel segments roughly: without the cue speaker's words, anyone's are used.
    assert [w.start for w in cue_words("So, ready?", 1.0, 1.6, words, speaker="S9")] == [1.0, 1.3]


def test_cue_words_share_the_gap_for_tokens_without_a_word():
    words = [Word(" one", 0.0, 0.5), Word(" four", 2.0, 2.5)]
    result = cue_words("one two three four", 0.0, 2.5, words)
    assert [(w.text, round(w.start, 3), round(w.end, 3)) for w in result] == [
        ("one", 0.0, 0.5),
        ("two", 0.5, 1.25),
        ("three", 1.25, 2.0),
        ("four", 2.0, 2.5),
    ]
    trailing = cue_words("one two", 0.0, 1.0, [Word(" one", 0.0, 0.4)])
    assert [(w.start, w.end) for w in trailing] == [(0.0, 0.4), (0.4, 1.0)]


def test_cue_words_without_any_matching_word_is_empty():
    assert cue_words("Hello there", 0.0, 1.0, [Word(" goodbye", 0.1, 0.5)]) == []
    assert cue_words("Hello there", 0.0, 1.0, [Word(" Hello", 5.0, 5.5)]) == []  # outside the cue's window
    assert cue_words("   ", 0.0, 1.0, [Word(" Hello", 0.0, 0.5)]) == []


# --- word -> frame steps within a cue


def test_steps_hold_short_pauses_and_show_long_ones_unlit():
    words = [cw("one", 1.0, 1.4), cw("two", 1.5, 1.9), cw("three", 2.6, 2.9)]
    steps = highlight_steps(24, 48, words, CLOCK, hold=6)
    # one: 24..34, held over the 2-frame pause; two: 36..46; 16-frame pause unlit; three: 62..70, held to the end.
    assert frames(steps) == [(24, 12, "one"), (36, 10, "two"), (46, 16, None), (62, 10, "three")]
    unheld = highlight_steps(24, 48, words, CLOCK, hold=0)
    assert frames(unheld) == [
        (24, 10, "one"),
        (34, 2, None),
        (36, 10, "two"),
        (46, 16, None),
        (62, 8, "three"),
        (70, 2, None),
    ]


def test_steps_lead_in_is_pulled_to_the_cue_start_only_when_short():
    # "a" starts 2 frames in (pulled to the cue start); 12 silent frames after it are too long to hold.
    assert frames(highlight_steps(24, 24, [cw("a", 1.1, 1.5)], CLOCK, hold=3)) == [(24, 12, "a"), (36, 12, None)]
    # 12 frames before "a" stay unlit; the 2 after it are held.
    assert frames(highlight_steps(24, 24, [cw("a", 1.5, 1.9)], CLOCK, hold=3)) == [(24, 12, None), (36, 12, "a")]


def test_steps_clamp_words_spanning_the_cue_boundaries():
    # The cue was trimmed to 24..44 (the next cue starts there); "one" began before the cue's first frame.
    words = [cw("one", 0.9, 1.3), cw("two", 1.5, 2.0), cw("three", 2.1, 2.5), cw("before", 0.2, 0.5)]
    assert frames(highlight_steps(24, 20, words, CLOCK, hold=0)) == [(24, 7, "one"), (31, 5, None), (36, 8, "two")]
    # A word ending exactly on the cue's first frame belongs to the cue before.
    assert frames(highlight_steps(24, 12, [cw("prev", 0.5, 1.0), cw("x", 1.0, 1.5)], CLOCK)) == [(24, 12, "x")]


def test_steps_give_sub_frame_words_a_frame_each():
    words = [cw("a", 1.0, 1.01), cw("b", 1.01, 1.02), cw("c", 1.02, 1.5)]
    assert frames(highlight_steps(24, 24, words, CLOCK, hold=0)) == [
        (24, 1, "a"),
        (25, 1, "b"),
        (26, 10, "c"),
        (36, 12, None),
    ]
    # More words than frames: the ones that don't fit are dropped, the cue is still covered.
    many = [cw(str(i), 1.0, 1.0) for i in range(5)]
    assert frames(highlight_steps(24, 3, many, CLOCK)) == [(24, 1, "0"), (25, 1, "1"), (26, 1, "2")]


def test_steps_without_words_cover_the_cue_once_and_respect_the_origin():
    assert highlight_steps(100, 30, [], CLOCK) == [HighlightStep(100, 30, None)]
    clock = TimelineClock(fps=24.0, start_frame=86400)
    steps = highlight_steps(86400 + 24, 24, [cw("a", 0.0, 0.5)], clock, origin_frame=86400 + 24, hold=0)
    assert frames(steps) == [(86424, 12, "a"), (86436, 12, None)]


def test_steps_are_contiguous_and_ordered_for_random_cues():
    rng = random.Random(7)
    for _ in range(300):
        record, duration = rng.randint(0, 500), rng.randint(1, 120)
        words = []
        t = record / 24.0 + rng.uniform(-0.5, 0.5)
        for index in range(rng.randint(0, 12)):
            length = rng.choice([0.0, 0.01, rng.uniform(0.05, 0.8)])
            words.append(cw(str(index), t, t + length))
            t += length + rng.choice([0.0, rng.uniform(0.0, 0.6)])
        steps = highlight_steps(record, duration, words, CLOCK, hold=rng.randint(0, 12))
        assert steps[0].record_frame == record and steps[-1].end_frame == record + duration
        assert all(a.end_frame == b.record_frame for a, b in pairwise(steps))
        assert all(s.duration >= 1 for s in steps)
        lit = [s.word.text for s in steps if s.word]
        assert lit == sorted(set(lit), key=int)  # each word at most once, in spoken order


# --- planning


def _transcript_with_words():
    data = {
        "language": "en",
        "duration": 5.0,
        "schema_version": 1,
        "speakers": [{"id": "S0", "name": "Maria"}, {"id": "S1", "name": None}],
        "segments": [
            {
                "speaker": "S0",
                "start": 0.5,
                "end": 2.0,
                "text": "Hey Jordan",
                "words": [
                    {"text": " Hey", "start": 0.5, "end": 0.8},
                    {"text": " Jordan", "start": 0.9, "end": 2.0},
                    {"text": " ", "start": 2.0, "end": 2.0},
                ],
            },
            {
                "speaker": "S0",
                "start": 1.75,
                "end": 3.0,
                "text": "Ready now",
                "words": [
                    {"text": " Ready", "start": 1.75, "end": 2.3},
                    {"text": " now", "start": 2.4, "end": 3.0, "speaker": "S0"},
                ],
            },
        ],
        "cues": [
            {"speaker": "S0", "start": 0.5, "end": 2.0, "lines": ["Hey Jordan"]},
            {"speaker": "S0", "start": 1.75, "end": 3.0, "lines": ["Ready now"]},  # overlaps: the first cue is trimmed
            {"speaker": "S1", "start": 3.5, "end": 4.0, "lines": ["Untimed words"]},
        ],
    }
    return from_dict(data)


def test_transcript_reads_the_segments_words():
    transcript = _transcript_with_words()
    assert [(w.text, w.start, w.speaker) for w in transcript.words] == [
        (" Hey", 0.5, "S0"),
        (" Jordan", 0.9, "S0"),
        (" Ready", 1.75, "S0"),
        (" now", 2.4, "S0"),
    ]


def test_plan_tracks_adds_word_steps_from_the_trimmed_frames():
    transcript = _transcript_with_words()
    plans = planning.plan_tracks(transcript, {}, planning.MODE_TEXTPLUS, CLOCK, origin_frame=0, highlight=True)
    maria, other = plans
    assert maria.highlight and other.highlight
    first, second = maria.clips
    assert (first.record_frame, first.duration) == (12, 30)  # trimmed: ends where "Ready now" starts (frame 42)
    assert frames(first.steps) == [(12, 10, "Hey"), (22, 20, "Jordan")]  # "Jordan" (22..48) cut at the trim
    assert frames(second.steps) == [(42, 16, "Ready"), (58, 14, "now")]  # the 3-frame pause (55..58) is held
    assert other.clips[0].steps == ()  # no timed words: one plain clip
    plain = planning.plan_tracks(transcript, {}, planning.MODE_TEXTPLUS, CLOCK, origin_frame=0)
    assert not plain[0].highlight and all(clip.steps == () for clip in plain[0].clips)
    subtitles = planning.plan_tracks(transcript, {}, planning.MODE_SUBTITLES, CLOCK, origin_frame=0, highlight=True)
    assert not subtitles[0].highlight and subtitles[0].clips[0].steps == ()


# --- the comp edit


def _exported(text="Hey Jordan,\nhow are you?", styled_text_source=None, duration=48):
    tool = FakeTool("TextPlus", "Template")
    tool.SetInput("StyledText", text)
    return FakeComp([tool], styled_text_source=styled_text_source).comp_text(duration)


def test_add_highlight_links_a_cls_tool_and_keeps_the_rest_of_the_comp():
    comp = _exported()
    entries = word_highlight.cls_entries(4, 10, (1.0, 0.831373, 0.0))
    edited = add_highlight_to_comp(comp, 'Hey "Jordan",\nhow are you?', entries)
    parsed = parse_imported_comp(edited)
    assert parsed is not None
    assert parsed.cls == {
        "tool": "BetterCaptionsHighlight",
        "text": 'Hey "Jordan",\nhow are you?',
        "array": [(CLS_RED, 4, 10, 1.0), (CLS_GREEN, 4, 10, 0.831373), (CLS_BLUE, 4, 10, 0.0)],
    }
    # Only the StyledText input changed and one tool was added; the template's look is untouched.
    removed = [line for line in comp.splitlines() if line not in edited.splitlines()]
    assert removed == ['\t\t\t\tStyledText = Input { Value = "Hey Jordan,\\nhow are you?", },']
    for kept in (
        'Font = Input { Value = "Open Sans", },',
        "Red1 = Input { Value = 0.95, },",
        "TEMPLATE_ID",
        '"C:\\\\Cache {v2} = TextPlus {"',
    ):
        assert kept in edited
    assert edited.index("BetterCaptionsHighlight = StyledTextCLS {") < edited.index("Template = TextPlus {")


def test_add_highlight_handles_missing_inputs_and_name_clashes():
    comp = (
        "Composition {\n\tTools = ordered() {\n\t\tBetterCaptionsHighlight = Background { },\n"
        "\t\tT = TextPlus {\n\t\t},\n\t},\n}\n"
    )
    edited = add_highlight_to_comp(comp, "hi", word_highlight.cls_entries(0, 1, (1, 0, 0)))
    assert "BetterCaptionsHighlight2 = StyledTextCLS {" in edited
    assert 'SourceOp = "BetterCaptionsHighlight2"' in edited and "Inputs = {" in edited
    no_styled = (
        "Composition {\n\tTools = {\n\t\tT = TextPlus {\n\t\t\tInputs = {\n"
        "\t\t\t\tSize = Input { Value = 0.1, },\n\t\t\t},\n\t\t},\n\t},\n}\n"
    )
    edited = parse_imported_comp(add_highlight_to_comp(no_styled, "hi", word_highlight.cls_entries(0, 1, (1, 0, 0))))
    assert edited is not None and edited.cls["text"] == "hi"


def test_add_highlight_into_a_macro_puts_the_tool_next_to_the_text():
    comp = (
        "Composition {\n\tTools = ordered() {\n\t\tMyTitle = MacroOperator {\n\t\t\tTools = ordered() {\n"
        '\t\t\t\tTemplate = TextPlus {\n\t\t\t\t\tInputs = {\n\t\t\t\t\t\tStyledText = Input { Value = "x", },\n'
        "\t\t\t\t\t},\n\t\t\t\t},\n\t\t\t},\n\t\t},\n\t},\n}\n"
    )
    edited = add_highlight_to_comp(comp, "x", word_highlight.cls_entries(0, 0, (1, 1, 0)))
    inner = edited.index("Tools = ordered() {", edited.index("MacroOperator"))
    assert inner < edited.index("BetterCaptionsHighlight = StyledTextCLS") < edited.index("Template = TextPlus")
    assert parse_imported_comp(edited) is not None


def test_add_highlight_refuses_templates_that_drive_their_own_text():
    with pytest.raises(HighlightUnsupported, match="its own Follower1 tool"):
        add_highlight_to_comp(_exported(styled_text_source="Follower1"), "x", [])
    expression = (
        "Composition {\n\tTools = {\n\t\tT = TextPlus {\n\t\t\tInputs = {\n"
        '\t\t\t\tStyledText = Input { Expression = "x", },\n\t\t\t},\n\t\t},\n\t},\n}'
    )
    with pytest.raises(HighlightUnsupported, match="an expression"):
        add_highlight_to_comp(expression, "x", [])
    with pytest.raises(HighlightUnsupported, match="no Text\\+ tool"):
        add_highlight_to_comp(
            "Composition {\n\tTools = {\n"
            '\t\tB = Background { Inputs = { N = Input { Value = "T = TextPlus {" } } },\n\t},\n}',
            "x",
            [],
        )
    with pytest.raises(HighlightUnsupported, match="unterminated"):
        add_highlight_to_comp(
            'Composition { Tools = { T = TextPlus { Inputs = { StyledText = Input { Value = "x', "x", []
        )


def test_lua_string_escapes():
    assert word_highlight.lua_string('a "b" \\ c\nd\te\x01é') == '"a \\"b\\" \\\\ c\\nd\\te\\001é"'


# --- the Resolve calls


def _plan_with_steps(resolve, template_item, texts=(("Hey Jordan", 1.0, 2.0), ("Ready now", 3.0, 4.0))):
    tl = resolve.project.timeline
    clock = TimelineClock(fps=24.0, start_frame=tl.start_frame)
    words, cues = [], []
    for text, start, end in texts:
        tokens = text.split()
        step = (end - start) / len(tokens)
        words += [Word(" " + t, start + i * step, start + (i + 1) * step, "S0") for i, t in enumerate(tokens)]
        cues.append({"speaker": "S0", "start": start, "end": end, "lines": [text]})
    transcript = from_dict(
        {"language": "en", "duration": 5.0, "speakers": [{"id": "S0", "name": "Maria"}], "cues": cues}
    )
    transcript.words = words
    plan = planning.plan_tracks(
        transcript, {}, planning.MODE_TEXTPLUS, clock, highlight=True, templates={"S0": template_item}
    )[0]
    return plan


def _template(resolve, name="Caption Style A"):
    return next(t for t in resolve_io.find_text_templates(resolve.project.media_pool) if t.name == name)


def test_textplus_track_with_highlight_places_one_clip_per_word_step(tmp_path):
    resolve = make_world()
    tl, pool = resolve.project.timeline, resolve.project.media_pool
    plan = _plan_with_steps(resolve, _template(resolve))
    style = resolve_io.HighlightStyle(rgb=(1.0, 0.831373, 0.0), work_dir=str(tmp_path / "hl"))
    result = resolve_io.create_textplus_track(pool, tl, plan, highlight=style)

    items = tl.track("video", "Maria").items
    start = tl.start_frame
    assert [(i.GetStart() - start, i.GetDuration()) for i in items] == [(24, 12), (36, 12), (72, 12), (84, 12)]
    assert result.placed == 4 and result.highlighted == 4 and not result.misplaced and not result.notes
    assert [i.text() for i in items] == [
        "Hey Jordan",
        "Hey Jordan",
        "Ready now",
        "Ready now",
    ]  # the whole cue each time
    assert [i.highlight()["array"][0][1:3] for i in items] == [(0, 2), (4, 9), (0, 4), (6, 8)]  # the spoken word
    assert all([e[0] for e in i.highlight()["array"]] == [CLS_RED, CLS_GREEN, CLS_BLUE] for i in items)
    assert items[0].highlight()["array"][1][3] == 0.831373
    assert all(i.media_pool_item.name == "Caption Style A" and i.loaded == ["Composition 2"] for i in items)
    assert os.listdir(str(tmp_path / "hl")) == []  # edited comps are removed after the import


def test_highlight_keeps_unlit_pauses_plain_and_works_when_import_replaces_the_comp(tmp_path, monkeypatch):
    resolve = make_world()
    tl, pool = resolve.project.timeline, resolve.project.media_pool
    plan = _plan_with_steps(resolve, _template(resolve), texts=(("Hey there", 1.0, 3.0),))
    word = plan.clips[0].steps[0].word
    plan.clips[0] = dataclasses.replace(
        plan.clips[0],
        steps=(HighlightStep(tl.start_frame + 24, 12, word), HighlightStep(tl.start_frame + 36, 36, None)),
    )
    original_init = FakeTimelineItem.__init__

    def replacing(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self.import_replaces = True

    monkeypatch.setattr(FakeTimelineItem, "__init__", replacing)
    result = resolve_io.create_textplus_track(
        pool, tl, plan, highlight=resolve_io.HighlightStyle((1, 0, 0), str(tmp_path))
    )
    lit, pause = tl.track("video", "Maria").items
    assert result.highlighted == 1 and lit.highlight()["array"][0] == (CLS_RED, 0, 2, 1.0) and lit.loaded == []
    assert pause.highlight() is None and pause.text() == "Hey there" and pause.exported == []


def test_highlight_falls_back_to_plain_clips_when_the_template_drives_its_own_text(tmp_path):
    resolve = make_world()
    tl, pool = resolve.project.timeline, resolve.project.media_pool
    animated = FakeMediaPoolItem("Typewriter", kind="Generator", styled_text_source="Follower1")
    plan = _plan_with_steps(resolve, animated)
    result = resolve_io.create_textplus_track(
        pool, tl, plan, highlight=resolve_io.HighlightStyle((1, 0, 0), str(tmp_path))
    )
    items = tl.track("video", "Maria").items
    assert [(i.GetStart() - tl.start_frame, i.GetDuration(), i.text()) for i in items] == [
        (24, 24, "Hey Jordan"),
        (72, 24, "Ready now"),
    ]
    assert result.highlighted == 0 and result.notes == [
        "words not highlighted: the template's text is driven by its own Follower1 tool"
    ]
    assert [t.name for t in tl.tracks["video"]] == ["Video 1", "Maria"] and tl.deleted_tracks == [("video", 2, "Maria")]


def test_highlight_falls_back_when_resolve_cannot_export_comps(tmp_path, monkeypatch):
    resolve = make_world()
    tl, pool = resolve.project.timeline, resolve.project.media_pool
    monkeypatch.setattr(FakeTimelineItem, "ExportFusionComp", lambda self, path, index: False)
    result = resolve_io.create_textplus_track(
        pool,
        tl,
        _plan_with_steps(resolve, _template(resolve)),
        highlight=resolve_io.HighlightStyle((1, 0, 0), str(tmp_path)),
    )
    assert len(tl.track("video", "Maria").items) == 2 and result.notes == [
        "words not highlighted: Resolve did not export the clip's Fusion comp"
    ]

    def boom(self, path, index):
        raise AttributeError("ExportFusionComp")

    monkeypatch.setattr(FakeTimelineItem, "ExportFusionComp", boom)
    result = resolve_io.create_textplus_track(
        pool,
        tl,
        _plan_with_steps(resolve, _template(resolve)),
        highlight=resolve_io.HighlightStyle((1, 0, 0), str(tmp_path)),
    )
    assert "could not export" in result.notes[0] and result.placed == 2


def test_highlight_failure_after_the_first_clip_is_reported_per_clip(tmp_path, monkeypatch):
    resolve = make_world()
    tl, pool = resolve.project.timeline, resolve.project.media_pool
    calls = {"n": 0}
    real_import = FakeTimelineItem.ImportFusionComp

    def flaky(self, path):
        calls["n"] += 1
        return None if calls["n"] == 2 else real_import(self, path)

    monkeypatch.setattr(FakeTimelineItem, "ImportFusionComp", flaky)
    result = resolve_io.create_textplus_track(
        pool,
        tl,
        _plan_with_steps(resolve, _template(resolve)),
        highlight=resolve_io.HighlightStyle((1, 0, 0), str(tmp_path)),
    )
    items = tl.track("video", "Maria").items
    assert result.highlighted == 3 and result.highlight_failures == [
        "Hey Jordan (Jordan): Resolve did not import the highlighted comp"
    ]
    assert items[1].highlight() is None and items[1].text() == "Hey Jordan"  # still shows the caption, unlit
    assert tl.deleted_tracks == []


def test_highlight_off_places_one_clip_per_cue_even_with_steps(tmp_path):
    resolve = make_world()
    tl, pool = resolve.project.timeline, resolve.project.media_pool
    result = resolve_io.create_textplus_track(pool, tl, _plan_with_steps(resolve, _template(resolve)))
    assert result.placed == 2 and all(i.exported == [] for i in tl.track("video", "Maria").items)


# --- app: config, review request, whole flow


def test_review_request_takes_the_remembered_highlight_settings():
    clock = TimelineClock(fps=24.0, start_frame=0)
    transcript = from_dict(TRANSCRIPT)

    def request(config):
        return app.build_review_request(transcript, [], "TL", clock, 0, None, config)

    assert (request({}).highlight_words, request({}).highlight_color) == (False, "#FFD400")  # off by default
    remembered = request({"highlight_words": True, "highlight_color": "#00ff88"})
    assert (remembered.highlight_words, remembered.highlight_color) == (True, "#00FF88")
    assert request({"highlight_words": "yes", "highlight_color": "green"}).highlight_color == "#FFD400"
    assert request({"highlight_words": "yes"}).highlight_words is False


def test_review_choices_are_saved_in_config_json_keeping_other_settings(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"engine": ["py"], "naming_provider": "llm"}), encoding="utf-8")
    config = config_mod.load_config(str(path))
    outcome = app.Outcome()
    app.remember_review_choices(config, ReviewResult(highlight_words=True, highlight_color="#00FF88"), outcome)
    assert json.loads(path.read_text(encoding="utf-8")) == {
        "engine": ["py"],
        "naming_provider": "llm",
        "highlight_words": True,
        "highlight_color": "#00FF88",
    }
    assert config["highlight_words"] is True and not outcome.warnings
    assert config_mod.save_settings(config, {"highlight_words": True}) is False  # unchanged: not rewritten
    assert not os.path.exists(str(path) + ".tmp")

    path.write_text("{broken", encoding="utf-8")
    app.remember_review_choices(config, ReviewResult(highlight_words=False), outcome)
    assert path.read_text(encoding="utf-8") == "{broken" and "not remembered" in outcome.warnings[0]


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "config.json"
    data = {"engine": [sys.executable, "-I", FAKE_ENGINE], "engine_args": ["--with-words"]}
    path.write_text(json.dumps(data), encoding="utf-8")
    return config_mod.load_config(str(path))


def test_full_flow_highlights_words_and_remembers_the_choice(config):
    resolve = make_world()
    backend = FakeBackend(accept_all(subtitles=False, textplus=True, highlight_words=True, highlight_color="#FF0000"))
    outcome = app.main(resolve=resolve, backend=backend, config=config, sleep=lambda s: None)
    assert outcome.error is None, backend.errors
    tl = resolve.project.timeline
    maria = tl.track("video", "Maria").items
    words = [w for s in with_words(TRANSCRIPT)["segments"] if s["speaker"] == "SPEAKER_00" for w in s["words"]]
    assert len(maria) == len(words) == 9  # "Hey Jordan, / how are you?" + "Ready for the race?": one clip per word
    assert [i.highlight()["text"] for i in maria[:2]] == ["Hey Jordan,\nhow are you?"] * 2
    assert [i.highlight()["array"][0] for i in maria[:2]] == [(CLS_RED, 0, 2, 1.0), (CLS_RED, 4, 10, 1.0)]
    assert all(i.highlight()["array"][1] == (CLS_GREEN, *i.highlight()["array"][1][1:3], 0.0) for i in maria)
    assert maria[0].GetStart() == tl.start_frame + 12 and maria[-1].GetEnd() == tl.start_frame + 156
    assert "spoken word highlighted in 9 clips" in backend.messages[0][1]
    with open(config["_path"], encoding="utf-8") as handle:
        saved = json.load(handle)
    assert (
        saved["highlight_words"] is True
        and saved["highlight_color"] == "#FF0000"
        and saved["engine_args"] == ["--with-words"]
    )
    # The next run's dialog starts from the remembered choice.
    second = FakeBackend(None)
    app.main(resolve=make_world(), backend=second, config=config_mod.load_config(config["_path"]), sleep=lambda s: None)
    assert (second.requests[0].highlight_words, second.requests[0].highlight_color) == (True, "#FF0000")


def test_full_flow_without_word_timings_places_plain_captions(config):
    config["engine_args"] = []
    resolve = make_world()
    backend = FakeBackend(accept_all(subtitles=False, textplus=True, highlight_words=True))
    outcome = app.main(resolve=resolve, backend=backend, config=config, sleep=lambda s: None)
    assert outcome.error is None, backend.errors
    assert len(resolve.project.timeline.track("video", "Maria").items) == 2
    assert "Warning: no word timings for Maria's captions; placed without highlighting" in backend.messages[0][1]


def test_highlight_is_ignored_for_subtitle_only_runs(config):
    resolve = make_world()
    backend = FakeBackend(accept_all(subtitles=True, textplus=False, highlight_words=True))
    outcome = app.main(resolve=resolve, backend=backend, config=config, sleep=lambda s: None)
    assert outcome.error is None and resolve.project.timeline.GetTrackCount("subtitle") == 2
    assert [t.name for t in resolve.project.timeline.tracks["video"]] == ["Video 1"]


# --- the review dialog, both backends


def _request(**kwargs):
    return ReviewRequest(
        speakers=[SpeakerRow("SPEAKER_00", "Maria", 2, "Hey")], templates=["Style A  (Master)"], **kwargs
    )


def test_uimanager_review_has_the_highlight_toggle_and_colour():
    def script(window):
        items = window.GetItems()
        assert items["Highlight"].Checked is False and items["HighlightColor"].Text == "#FFD400"
        items["TextPlus"].Checked = True
        items["Highlight"].Checked = True
        items["HighlightColor"].Text = "nope"
        window.On.Ok.Clicked()  # refused: not a colour
        assert "#FFD400" in items["ReviewStatus"].Text
        items["HighlightColor"].Text = "0f8"
        window.On.Ok.Clicked()

    result = UIManagerBackend(FakeFusion(), FakeBmd(script)).review(_request())
    assert result.highlight_words is True and result.highlight_color == "#00FF88" and result.textplus is True


def test_uimanager_review_starts_from_the_remembered_choice_and_keeps_it_across_fix_speakers():
    from tests.resolve.test_ui import _editable_request

    seen = {"reviews": 0}

    def script(window):
        items = window.GetItems()
        if window.props["ID"] == "BetterCaptionsSpeakers":
            window.On.Done.Clicked()
            return
        seen["reviews"] += 1
        if seen["reviews"] == 1:
            assert items["Highlight"].Checked is True and items["HighlightColor"].Text == "#112233"
            items["HighlightColor"].Text = "#445566"
            window.On.FixSpeakers.Clicked()
        else:
            assert items["Highlight"].Checked is True and items["HighlightColor"].Text == "#445566"
            items["Highlight"].Checked = False
            items["HighlightColor"].Text = "oops"  # ignored: highlighting is off
            window.On.Ok.Clicked()

    request = _editable_request()
    request.highlight_words, request.highlight_color = True, "#112233"
    result = UIManagerBackend(FakeFusion(), FakeBmd(script)).review(request)
    assert (result.highlight_words, result.highlight_color) == (False, "#112233")


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


def test_tk_review_highlight_toggle_colour_and_state(tk_root, monkeypatch):
    from better_captions_plugin.ui import tk_ui

    warnings = []
    monkeypatch.setattr(tk_ui.messagebox, "showwarning", lambda *args, **kwargs: warnings.append(args[1]))
    dialog = tk_ui._ReviewDialog(tk_root, _request(highlight_words=True, highlight_color="#112233"))
    assert dialog.highlight_var.get() is True and dialog.highlight_color_var.get() == "#112233"
    assert all(w.instate(["disabled"]) for w in dialog.highlight_widgets)  # Text+ not ticked yet
    dialog.textplus_var.set(True)
    assert all(not w.instate(["disabled"]) for w in dialog.highlight_widgets)
    dialog.highlight_color_var.set("purple")
    dialog._ok()
    assert dialog.result is None and "#FFD400" in warnings[0]
    dialog.highlight_color_var.set("#abc")
    dialog._ok()
    assert dialog.result.highlight_words is True and dialog.result.highlight_color == "#AABBCC"
