"""Frame/timecode arithmetic (pure functions, no Resolve objects).

Resolve's API addresses timeline positions in absolute frames: ``Timeline.GetStartFrame()``
is the frame number of the timeline's start timecode (e.g. 86400 for 01:00:00:00 at 24 fps),
and ``recordFrame`` in ``AppendToTimeline`` and ``TimelineItem.GetStart()`` live in that
same space. Engine times are seconds from the start of the rendered audio. So

    frame = range_start_frame + round(seconds * fps)

Drop-frame only changes how a frame count is *labelled* as a timecode (frame numbers
00 and 01 are skipped at the start of every minute not divisible by 10, for 29.97/59.94),
which matters when a timecode string has to be converted to frames or back.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_TC_RE = re.compile(r"^\s*(\d{1,2})[:;.](\d{2})[:;.](\d{2})([:;.,])(\d{2,3})\s*$")


def parse_frame_rate(value) -> tuple[float, bool]:
    """Parse a Resolve frame-rate setting into ``(fps, drop_frame)``.

    ``Timeline.GetSetting("timelineFrameRate")`` returns strings like ``"24"``,
    ``"29.97"`` or ``"29.97 DF"``; numbers are accepted too.
    """
    if value is None:
        raise ValueError("no frame rate")
    if isinstance(value, (int, float)):
        return float(value), False
    text = str(value).strip()
    drop = text.upper().endswith("DF")
    if drop:
        text = text[:-2].strip()
    if not text:
        raise ValueError("empty frame rate %r" % (value,))
    fps = float(text)
    if fps <= 0:
        raise ValueError("invalid frame rate %r" % (value,))
    return fps, drop


def nominal_rate(fps: float) -> int:
    """The integer timecode base for a frame rate: 30 for 29.97, 24 for 23.976, ..."""
    return int(round(fps))


def drop_frames_per_minute(fps: float) -> int:
    """Frames skipped per non-tenth minute in drop-frame timecode (2 at 29.97, 4 at 59.94)."""
    return int(round(fps * 0.066666))


def is_drop_frame_rate(fps: float) -> bool:
    """Drop-frame timecode only exists for the NTSC 29.97/59.94 family."""
    return abs(fps - nominal_rate(fps)) > 1e-6 and nominal_rate(fps) % 30 == 0


def seconds_to_frames(seconds: float, fps: float) -> int:
    return int(round(seconds * fps))


def frames_to_seconds(frames: int, fps: float) -> float:
    return frames / fps


def timecode_to_frames(timecode: str, fps: float, drop_frame: bool = False) -> int:
    """``HH:MM:SS:FF`` (or ``;FF`` / ``,FF``) to a frame count from 00:00:00:00."""
    match = _TC_RE.match(str(timecode))
    if not match:
        raise ValueError("bad timecode %r" % (timecode,))
    hh, mm, ss, _sep, ff = match.groups()
    hours, minutes, secs, frames = int(hh), int(mm), int(ss), int(ff)
    base = nominal_rate(fps)
    if frames >= base and len(ff) <= 2:
        raise ValueError("frame field %s out of range for %s fps" % (ff, fps))
    total = ((hours * 60 + minutes) * 60 + secs) * base + frames
    if drop_frame and is_drop_frame_rate(fps):
        total_minutes = hours * 60 + minutes
        total -= drop_frames_per_minute(fps) * (total_minutes - total_minutes // 10)
    return total


def frames_to_timecode(frames: int, fps: float, drop_frame: bool = False) -> str:
    """Frame count from 00:00:00:00 to ``HH:MM:SS:FF`` (``HH:MM:SS;FF`` when drop-frame)."""
    if frames < 0:
        raise ValueError("negative frame count %d" % frames)
    base = nominal_rate(fps)
    separator = ":"
    if drop_frame and is_drop_frame_rate(fps):
        separator = ";"
        dropped = drop_frames_per_minute(fps)
        per_ten_minutes = int(round(fps * 600))
        per_minute = base * 60 - dropped
        tens, rest = divmod(frames, per_ten_minutes)
        frames += dropped * 9 * tens
        if rest > dropped:
            frames += dropped * ((rest - dropped) // per_minute)
    ff = frames % base
    total_seconds = frames // base
    ss = total_seconds % 60
    mm = (total_seconds // 60) % 60
    hh = total_seconds // 3600
    return "%02d:%02d:%02d%s%02d" % (hh, mm, ss, separator, ff)


@dataclass(frozen=True)
class TimelineClock:
    """What is needed to map engine seconds onto a Resolve timeline."""

    fps: float
    drop_frame: bool = False
    start_frame: int = 0  # Timeline.GetStartFrame(): absolute frame of the first timeline frame

    @classmethod
    def from_settings(
        cls, frame_rate, start_timecode: str | None = None, start_frame: int | None = None, drop_frame_setting=None
    ) -> TimelineClock:
        """Build from raw Resolve values; ``start_frame`` wins over ``start_timecode``."""
        fps, drop = parse_frame_rate(frame_rate)
        if drop_frame_setting is not None:
            drop = drop or str(drop_frame_setting).strip().lower() in ("1", "true", "yes", "on")
        if start_timecode and ";" in str(start_timecode):
            drop = True
        drop = drop and is_drop_frame_rate(fps)
        if start_frame is None:
            start_frame = timecode_to_frames(start_timecode, fps, drop) if start_timecode else 0
        return cls(fps=fps, drop_frame=drop, start_frame=int(start_frame))

    def frame_at(self, seconds: float, origin_frame: int | None = None) -> int:
        """Absolute frame for ``seconds`` after ``origin_frame`` (default: the timeline start)."""
        origin = self.start_frame if origin_frame is None else origin_frame
        return origin + seconds_to_frames(seconds, self.fps)

    def timecode(self, frame: int) -> str:
        return frames_to_timecode(frame, self.fps, self.drop_frame)

    def frame_of_timecode(self, timecode: str) -> int:
        return timecode_to_frames(timecode, self.fps, self.drop_frame)
