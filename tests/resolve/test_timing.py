import pytest

from better_captions_plugin import timing
from better_captions_plugin.timing import TimelineClock, frames_to_timecode, parse_frame_rate, timecode_to_frames


@pytest.mark.parametrize(
    "value, expected",
    [
        ("24", (24.0, False)),
        ("23.976", (23.976, False)),
        ("29.97", (29.97, False)),
        ("29.97 DF", (29.97, True)),
        ("59.94DF", (59.94, True)),
        (25, (25.0, False)),
        (29.97, (29.97, False)),
    ],
)
def test_parse_frame_rate(value, expected):
    assert parse_frame_rate(value) == expected


@pytest.mark.parametrize("bad", [None, "", "DF", "0", "abc"])
def test_parse_frame_rate_rejects_garbage(bad):
    with pytest.raises(ValueError):
        parse_frame_rate(bad)


def test_non_drop_timecode_round_trip():
    assert timecode_to_frames("01:00:00:00", 24) == 86400
    assert timecode_to_frames("00:00:01:12", 24) == 36
    assert frames_to_timecode(86400 + 36, 24) == "01:00:01:12"
    # 23.976 and 29.97 non-drop count with the integer base
    assert timecode_to_frames("00:01:00:00", 23.976) == 1440
    assert timecode_to_frames("00:01:00:00", 29.97) == 1800
    assert frames_to_timecode(1800, 29.97) == "00:01:00:00"


def test_drop_frame_timecode_known_values():
    # Frames 00 and 01 are dropped at every minute not divisible by 10.
    assert timecode_to_frames("00:01:00;02", 29.97, True) == 1800
    assert timecode_to_frames("00:10:00;00", 29.97, True) == 17982
    assert timecode_to_frames("01:00:00;00", 29.97, True) == 107892
    assert frames_to_timecode(1800, 29.97, True) == "00:01:00;02"
    assert frames_to_timecode(17982, 29.97, True) == "00:10:00;00"
    assert frames_to_timecode(107892, 29.97, True) == "01:00:00;00"
    assert timecode_to_frames("00:01:00;04", 59.94, True) == 3600
    assert frames_to_timecode(3600, 59.94, True) == "00:01:00;04"


@pytest.mark.parametrize("fps, drop", [(29.97, True), (59.94, True), (24, False), (25, False), (23.976, False)])
def test_timecode_round_trip_sweep(fps, drop):
    for frame in list(range(0, 40000, 997)) + [1799, 1800, 1801, 17981, 17982, 17983, 107891, 107892]:
        tc = frames_to_timecode(frame, fps, drop)
        assert timecode_to_frames(tc, fps, drop) == frame, (frame, tc)


def test_drop_frame_flag_ignored_for_integer_rates():
    # "25 DF" makes no sense; treat it as non-drop rather than corrupting frame counts.
    assert timecode_to_frames("00:01:00:00", 25, True) == 1500
    assert frames_to_timecode(1500, 25, True) == "00:01:00:00"


def test_timecode_separators_and_validation():
    assert timecode_to_frames("00:00:10;05", 29.97, True) == timecode_to_frames("00:00:10:05", 29.97, True)
    with pytest.raises(ValueError):
        timecode_to_frames("00:00:10:30", 29.97)  # frame field out of range
    with pytest.raises(ValueError):
        timecode_to_frames("nope", 24)


def test_clock_from_settings_prefers_start_frame_and_detects_drop():
    clock = TimelineClock.from_settings("29.97", start_timecode="01:00:00;00", start_frame=107892)
    assert clock.drop_frame is True
    assert clock.start_frame == 107892
    assert clock.frame_at(1.0) == 107892 + 30
    # 60 s of audio is 1798 frames at 29.97, two frames short of the drop-frame label 01:01:00;02.
    assert clock.frame_at(60.0) == 107892 + 1798
    assert clock.timecode(107892 + 1798) == "01:00:59;28"
    assert clock.timecode(107892 + 1800) == "01:01:00;02"


def test_clock_from_settings_falls_back_to_start_timecode_and_setting():
    clock = TimelineClock.from_settings("29.97", start_timecode="01:00:00:00", drop_frame_setting="1")
    assert clock.drop_frame is True
    assert clock.start_frame == 107892
    clock = TimelineClock.from_settings("24", start_timecode="01:00:00:00")
    assert (clock.fps, clock.drop_frame, clock.start_frame) == (24.0, False, 86400)
    assert TimelineClock.from_settings("24").start_frame == 0


def test_frame_at_rounds_and_uses_origin():
    clock = TimelineClock(fps=24, start_frame=86400)
    assert clock.frame_at(0.5) == 86412
    assert clock.frame_at(0.02) == 86400  # 0.48 frames rounds down
    assert clock.frame_at(0.03) == 86401  # 0.72 frames rounds up
    assert clock.frame_at(1.0, origin_frame=90000) == 90024


def test_helpers():
    assert timing.nominal_rate(29.97) == 30
    assert timing.nominal_rate(23.976) == 24
    assert timing.drop_frames_per_minute(29.97) == 2
    assert timing.drop_frames_per_minute(59.94) == 4
    assert timing.is_drop_frame_rate(29.97) and timing.is_drop_frame_rate(59.94)
    assert not timing.is_drop_frame_rate(23.976) and not timing.is_drop_frame_rate(30)
    assert timing.frames_to_seconds(48, 24) == 2.0
