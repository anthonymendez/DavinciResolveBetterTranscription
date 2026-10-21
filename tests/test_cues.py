import pytest

from better_captions.cues import CueOptions, build_cues
from better_captions.schema import Segment, Transcript, Word


def seg(speaker, spec):
    """spec: [(text, start, end), ...] -> Segment with timed words."""
    words = [Word(t, s, e, speaker=speaker) for t, s, e in spec]
    return Segment(speaker, words[0].start, words[-1].end, " ".join(t for t, _, _ in spec), words)


def transcript(*segments):
    return Transcript(language="en", duration=20.0, segments=list(segments))


def evenly(text, start=0.0, step=0.5):
    return [(t, start + i * step, start + (i + 1) * step) for i, t in enumerate(text.split())]


def test_line_wrapping_respects_max_chars_and_max_lines():
    text = "the quick brown fox jumps over the lazy dog again and again"
    cues = build_cues(
        transcript(seg("S0", evenly(text))),
        CueOptions(max_chars_per_line=20, max_lines=2, max_duration=60.0),
    )
    assert len(cues) > 1
    for cue in cues:
        assert 1 <= len(cue.lines) <= 2
        assert all(len(line) <= 20 for line in cue.lines)
    # Every word survives, in order.
    assert " ".join(cue.text.replace("\n", " ") for cue in cues) == text


def test_lines_are_balanced():
    word = "a" * 9
    cues = build_cues(
        transcript(seg("S0", evenly(f"{word} {word} {word} {word}"))),
        CueOptions(max_chars_per_line=30, max_lines=2, max_duration=60.0),
    )
    assert cues[0].lines == [f"{word} {word}", f"{word} {word}"]


def test_breaks_after_punctuation_when_forced():
    # Plain greedy packing would give "gamma, delta"; the break should come after "gamma,".
    cues = build_cues(
        transcript(seg("S0", [("alpha", 0, 1), ("beta", 1, 2), ("gamma,", 2, 3), ("delta", 3, 4), ("epsilon", 4, 5)])),
        CueOptions(max_chars_per_line=14, max_lines=1, max_duration=60.0, min_duration=0.0),
    )
    assert [c.lines for c in cues] == [["alpha beta"], ["gamma,"], ["delta epsilon"]]


def test_long_single_word_is_never_split():
    long_word = "Supercalifragilistic"
    cues = build_cues(
        transcript(seg("S0", [("tiny", 0, 1), (long_word, 1, 2), ("tiny", 2, 3)])),
        CueOptions(max_chars_per_line=10, max_lines=1, max_duration=60.0, min_duration=0.0),
    )
    assert [c.lines for c in cues] == [["tiny"], [long_word], ["tiny"]]


def test_duration_splitting():
    cues = build_cues(
        transcript(seg("S0", evenly("w w w w w w w w w w", step=1.0))),
        CueOptions(max_chars_per_line=100, max_lines=2, max_duration=3.0, min_duration=0.0),
    )
    assert [(c.start, c.end) for c in cues] == [(0, 3), (3, 6), (6, 9), (9, 10)]
    assert all(c.end - c.start <= 3.0 for c in cues)


def test_single_word_longer_than_max_duration_still_gets_a_cue():
    cues = build_cues(
        transcript(seg("S0", [("long", 0.0, 10.0)])),
        CueOptions(max_duration=3.0, min_duration=0.0),
    )
    assert [(c.start, c.end, c.lines) for c in cues] == [(0.0, 10.0, ["long"])]


def test_short_cue_extended_to_min_duration():
    cues = build_cues(
        transcript(seg("S0", [("Hi.", 0.0, 0.1)])),
        CueOptions(min_duration=0.8),
    )
    assert cues[0].end == pytest.approx(0.8)


def test_extension_stops_before_next_same_speaker_cue():
    cues = build_cues(
        transcript(
            seg("S0", [("Hi.", 0.0, 0.1)]),
            seg("S0", [("Yo", 0.5, 0.6)]),
        ),
        CueOptions(min_duration=0.8, min_gap=0.05),
    )
    assert cues[0].end == pytest.approx(0.45)  # next cue starts at 0.5, minus min_gap
    assert cues[1].end == pytest.approx(1.3)  # no next cue, so full extension


def test_extension_ignores_other_speakers():
    cues = build_cues(
        transcript(
            seg("S0", [("Hi.", 0.0, 0.1)]),
            seg("S1", [("Yo", 0.5, 0.6)]),
        ),
        CueOptions(min_duration=0.8, min_gap=0.05),
    )
    assert cues[0].speaker == "S0"
    assert cues[0].end == pytest.approx(0.8)


def test_segment_text_fallback_when_words_empty():
    segment = Segment("S0", 0.0, 4.0, "one two three four", [])
    cues = build_cues(
        transcript(segment),
        CueOptions(max_duration=1.5, min_duration=0.0),
    )
    assert [c.lines for c in cues] == [["one"], ["two"], ["three"], ["four"]]
    assert [c.start for c in cues] == [0.0, 1.0, 2.0, 3.0]


def test_empty_segment_produces_no_cues():
    assert build_cues(transcript(Segment("S0", 0.0, 1.0, "   ", []))) == []


def test_cues_are_sorted_by_start():
    cues = build_cues(
        transcript(
            seg("S0", [("later", 5.0, 5.5)]),
            seg("S1", [("earlier", 1.0, 1.5)]),
        ),
        CueOptions(min_duration=0.0),
    )
    assert [c.start for c in cues] == [1.0, 5.0]
