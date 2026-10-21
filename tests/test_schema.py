from better_captions.schema import Cue, Segment, Speaker, Transcript, Word


def sample() -> Transcript:
    w = [Word("Hi", 0.0, 0.3, 0.9, "SPEAKER_00"), Word("Sam.", 0.3, 0.7, 0.95, "SPEAKER_00")]
    return Transcript(
        language="en",
        duration=1.0,
        speakers=[Speaker("SPEAKER_00", "Alex", 0.8, "llm"), Speaker("SPEAKER_01")],
        segments=[Segment("SPEAKER_00", 0.0, 0.7, "Hi Sam.", w)],
        cues=[Cue("SPEAKER_00", 0.0, 0.7, ["Hi Sam."])],
    )


def test_roundtrip(tmp_path):
    t = sample()
    t.save(tmp_path / "t.json")
    assert Transcript.load(tmp_path / "t.json") == t


def test_display_name():
    t = sample()
    assert t.speaker("SPEAKER_00").display_name == "Alex"
    assert t.speaker("SPEAKER_01").display_name == "SPEAKER_01"
