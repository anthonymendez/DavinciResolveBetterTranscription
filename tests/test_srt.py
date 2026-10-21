from better_captions.export.srt import format_timestamp, to_srt, write_speaker_srts
from better_captions.export.vtt import to_vtt
from better_captions.schema import Cue, Speaker, Transcript


def test_timestamp_formatting():
    assert format_timestamp(0) == "00:00:00,000"
    assert format_timestamp(1.5) == "00:00:01,500"
    assert format_timestamp(62.007) == "00:01:02,007"


def test_timestamp_past_one_hour():
    assert format_timestamp(3600) == "01:00:00,000"
    assert format_timestamp(3725.5) == "01:02:05,500"
    assert format_timestamp(36000.123) == "10:00:00,123"


def test_timestamp_uses_separator_for_vtt():
    assert format_timestamp(3725.5, ".") == "01:02:05.500"


def test_timestamp_clamps_negative_to_zero():
    assert format_timestamp(-0.5) == "00:00:00,000"


def test_to_srt_numbered_blocks():
    cues = [
        Cue("S0", 0.0, 1.25, ["Hello there"]),
        Cue("S0", 2.0, 3.5, ["Two", "lines"]),
    ]
    assert to_srt(cues) == (
        "1\n00:00:00,000 --> 00:00:01,250\nHello there\n"
        "\n"
        "2\n00:00:02,000 --> 00:00:03,500\nTwo\nlines\n"
    )


def test_to_srt_empty():
    assert to_srt([]) == ""


def test_write_speaker_srts_one_file_per_speaker_with_cues(tmp_path):
    t = Transcript(
        language="en",
        duration=10.0,
        speakers=[Speaker("SPEAKER_00"), Speaker("SPEAKER_01"), Speaker("SPEAKER_02")],
        cues=[
            Cue("SPEAKER_00", 0.0, 1.0, ["Café time"]),
            Cue("SPEAKER_01", 1.5, 2.0, ["Yes"]),
            Cue("SPEAKER_00", 3.0, 4.0, ["Again"]),
        ],
    )
    paths = write_speaker_srts(t, tmp_path / "out")

    assert set(paths) == {"SPEAKER_00", "SPEAKER_01"}
    assert paths["SPEAKER_00"] == tmp_path / "out" / "SPEAKER_00.srt"
    assert not (tmp_path / "out" / "SPEAKER_02.srt").exists()
    # Each file is numbered from 1 and only holds its own speaker's cues, UTF-8 encoded.
    assert paths["SPEAKER_00"].read_bytes().decode("utf-8") == (
        "1\n00:00:00,000 --> 00:00:01,000\nCafé time\n\n"
        "2\n00:00:03,000 --> 00:00:04,000\nAgain\n"
    )
    assert paths["SPEAKER_01"].read_text(encoding="utf-8") == (
        "1\n00:00:01,500 --> 00:00:02,000\nYes\n"
    )


def test_to_vtt_header_and_dot_separator():
    cues = [Cue("S0", 0.0, 3725.5, ["Hi", "there"])]
    assert to_vtt(cues) == "WEBVTT\n\n00:00:00.000 --> 01:02:05.500\nHi\nthere\n"


def test_to_vtt_empty_is_just_header():
    assert to_vtt([]) == "WEBVTT\n\n"
