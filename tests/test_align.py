from better_captions.engine.align import assign_speakers, group_segments
from better_captions.schema import SpeakerTurn, Word


def W(text, start, end, speaker=None):
    return Word(text, start, end, speaker=speaker)


def speakers(words):
    return [w.speaker for w in words]


# --- assign_speakers ---------------------------------------------------------


def test_no_turns_gives_single_default_speaker():
    words = [W("a", 0, 1), W("b", 5, 6)]
    assert speakers(assign_speakers(words, [])) == ["SPEAKER_00", "SPEAKER_00"]


def test_no_words():
    assert assign_speakers([], [SpeakerTurn("SPEAKER_00", 0, 1)]) == []


def test_word_inside_a_turn():
    turns = [SpeakerTurn("SPEAKER_00", 0, 2), SpeakerTurn("SPEAKER_01", 2, 4)]
    words = [W("a", 0.5, 1.0), W("b", 2.5, 3.0)]
    assert speakers(assign_speakers(words, turns)) == ["SPEAKER_00", "SPEAKER_01"]


def test_word_straddling_turns_goes_to_greatest_overlap():
    turns = [SpeakerTurn("SPEAKER_00", 0, 2), SpeakerTurn("SPEAKER_01", 2, 4)]
    assert speakers(assign_speakers([W("x", 1.9, 2.5)], turns)) == ["SPEAKER_01"]
    assert speakers(assign_speakers([W("x", 1.5, 2.1)], turns)) == ["SPEAKER_00"]


def test_overlapping_turns_pick_greatest_overlap():
    # A long turn with a short interjection inside it.
    turns = [SpeakerTurn("SPEAKER_00", 0, 10), SpeakerTurn("SPEAKER_01", 4, 4.3)]
    words = [W("long", 3.9, 4.6), W("yeah", 4.0, 4.3), W("more", 5, 5.5)]
    # "long" overlaps SPEAKER_00 for 0.7s vs SPEAKER_01 for 0.3s; "yeah" ties at 0.3s -> first turn by start.
    assert speakers(assign_speakers(words, turns)) == ["SPEAKER_00", "SPEAKER_00", "SPEAKER_00"]
    turns = [SpeakerTurn("SPEAKER_00", 0, 4.1), SpeakerTurn("SPEAKER_01", 4, 9)]
    assert speakers(assign_speakers([W("x", 4.0, 4.6)], turns)) == ["SPEAKER_01"]


def test_long_earlier_turn_still_found():
    # A turn that starts long before the word but is still running must be considered.
    turns = [SpeakerTurn("SPEAKER_00", 0, 100), SpeakerTurn("SPEAKER_01", 50, 51)]
    assert speakers(assign_speakers([W("x", 90, 91)], turns)) == ["SPEAKER_00"]


def test_word_in_gap_goes_to_nearest_turn():
    turns = [SpeakerTurn("SPEAKER_00", 0, 2), SpeakerTurn("SPEAKER_01", 5, 7)]
    words = [W("a", 2.2, 2.5), W("b", 4.6, 4.8), W("before", -1, -0.5), W("after", 9, 9.5)]
    assert speakers(assign_speakers(words, turns)) == ["SPEAKER_00", "SPEAKER_01", "SPEAKER_00", "SPEAKER_01"]


def test_nearest_uses_latest_ending_earlier_turn():
    # The previous turn by start (SPEAKER_01) ended long ago; SPEAKER_00 ended just before the word.
    turns = [SpeakerTurn("SPEAKER_00", 0, 9.9), SpeakerTurn("SPEAKER_01", 1, 2), SpeakerTurn("SPEAKER_02", 20, 21)]
    assert speakers(assign_speakers([W("x", 10, 10.2)], turns)) == ["SPEAKER_00"]


def test_zero_length_word_uses_containing_turn():
    turns = [SpeakerTurn("SPEAKER_00", 0, 2), SpeakerTurn("SPEAKER_01", 2, 4)]
    assert speakers(assign_speakers([W("x", 3, 3), W("y", 2, 2)], turns)) == ["SPEAKER_01", "SPEAKER_01"]


def test_unsorted_turns_and_inputs_not_mutated():
    turns = [SpeakerTurn("SPEAKER_01", 5, 7), SpeakerTurn("SPEAKER_00", 0, 2)]
    words = [W("a", 0.5, 1), W("b", 5.5, 6)]
    out = assign_speakers(words, turns)
    assert speakers(out) == ["SPEAKER_00", "SPEAKER_01"]
    assert speakers(words) == [None, None]
    assert [w.text for w in out] == ["a", "b"]


# --- group_segments ----------------------------------------------------------


def test_group_empty():
    assert group_segments([]) == []


def test_group_splits_on_speaker_change():
    words = [W("hi", 0, 0.3, "SPEAKER_00"), W("there", 0.4, 0.7, "SPEAKER_00"), W("yo", 0.8, 1, "SPEAKER_01")]
    segs = group_segments(words)
    assert [(s.speaker, s.text) for s in segs] == [("SPEAKER_00", "hi there"), ("SPEAKER_01", "yo")]
    assert (segs[0].start, segs[0].end) == (0, 0.7)
    assert [w.text for w in segs[0].words] == ["hi", "there"]


def test_group_splits_on_gap_over_max_gap():
    words = [W("a", 0, 0.5, "SPEAKER_00"), W("b", 1.5, 2, "SPEAKER_00"), W("c", 3.1, 3.5, "SPEAKER_00")]
    assert [s.text for s in group_segments(words, max_gap=1.0)] == ["a b", "c"]
    assert [s.text for s in group_segments(words, max_gap=2.0)] == ["a b c"]


def test_group_splits_after_sentence_end():
    texts = ["Hello", "there.", "How", "are", "you?", "Great!", "Wow…", '"Really?"', "ok"]
    words = [W(t, i, i + 0.5, "SPEAKER_00") for i, t in enumerate(texts)]
    assert [s.text for s in group_segments(words)] == ["Hello there.", "How are you?", "Great!", "Wow…", '"Really?"', "ok"]


def test_group_does_not_split_on_abbreviation_or_decimal():
    texts = ["Ask", "Mr.", "Smith", "about", "3.5", "things"]
    words = [W(t, i, i + 0.5, "SPEAKER_00") for i, t in enumerate(texts)]
    assert [s.text for s in group_segments(words)] == ["Ask Mr. Smith about 3.5 things"]


def test_group_words_without_speaker_default():
    segs = group_segments([W("a", 0, 1), W("b", 1, 2)])
    assert [(s.speaker, s.text) for s in segs] == [("SPEAKER_00", "a b")]


def test_group_joins_cjk_without_spaces():
    words = [W("你好", 0, 0.5, "SPEAKER_00"), W("世界", 0.5, 1, "SPEAKER_00"), W("OK", 1, 1.2, "SPEAKER_00")]
    assert group_segments(words)[0].text == "你好世界 OK"


def test_assign_then_group():
    turns = [SpeakerTurn("SPEAKER_00", 0, 1.5), SpeakerTurn("SPEAKER_01", 1.9, 3.0)]
    words = [W("Hey", 0, 0.25), W("Jordan,", 0.3, 0.55), W("Good,", 2.0, 2.25), W("thanks", 2.3, 2.55)]
    segs = group_segments(assign_speakers(words, turns))
    assert [(s.speaker, s.text) for s in segs] == [("SPEAKER_00", "Hey Jordan,"), ("SPEAKER_01", "Good, thanks")]
