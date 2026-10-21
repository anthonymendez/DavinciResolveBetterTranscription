"""Decide what goes on the timeline (pure functions; nothing here touches Resolve).

Given a transcript, the user's speaker names and the timeline clock, produce one
``TrackPlan`` per speaker: the track name (unique against existing tracks) and, for
Text+ mode, one ``ClipPlacement`` per cue with the absolute record frame and duration.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from better_captions_plugin.timing import TimelineClock
from better_captions_plugin.transcript import Cue, Transcript

MODE_SUBTITLES = "subtitles"
MODE_TEXTPLUS = "textplus"


@dataclass(frozen=True)
class ClipPlacement:
    record_frame: int  # absolute timeline frame
    duration: int  # frames, >= 1
    text: str
    start: float  # engine seconds, kept for messages
    end: float

    @property
    def end_frame(self) -> int:
        return self.record_frame + self.duration


@dataclass
class TrackPlan:
    speaker_id: str
    name: str  # track name == speaker display name (made unique)
    mode: str  # MODE_SUBTITLES or MODE_TEXTPLUS
    clips: "list[ClipPlacement]" = field(default_factory=list)
    template: object = None  # Text+ template chosen for this speaker (opaque)
    srt_path: "str | None" = None  # per-speaker SRT written by the engine

    @property
    def first_frame(self) -> "int | None":
        return self.clips[0].record_frame if self.clips else None


def clean_track_name(name: str, fallback: str = "Speaker") -> str:
    """Track names are free text in Resolve; just collapse whitespace and never return empty."""
    cleaned = re.sub(r"\s+", " ", str(name or "")).strip()
    return cleaned or fallback


def unique_name(name: str, taken: "set[str] | list[str]") -> str:
    """``name``, or ``name 2``, ``name 3``, ... until it differs (case-insensitively) from ``taken``."""
    lowered = {str(t).strip().lower() for t in taken}
    if name.lower() not in lowered:
        return name
    index = 2
    while ("%s %d" % (name, index)).lower() in lowered:
        index += 1
    return "%s %d" % (name, index)


def place_cues(cues: "list[Cue]", clock: TimelineClock, origin_frame: "int | None" = None) -> "list[ClipPlacement]":
    """Map a speaker's cues onto absolute frames, at least 1 frame each, never overlapping.

    ``origin_frame`` is the absolute frame where the rendered audio starts (the timeline start
    or the in-point). Cue starts are kept (that is what viewers notice); an earlier cue that
    overlaps the next one is trimmed to end where the next starts, and a cue that would start
    on the same frame as the previous one is pushed one frame later.
    """
    placements: "list[ClipPlacement]" = []
    previous_start = None
    for cue in sorted(cues, key=lambda c: (c.start, c.end)):
        start = clock.frame_at(cue.start, origin_frame)
        end = clock.frame_at(cue.end, origin_frame)
        if previous_start is not None and start <= previous_start:
            start = previous_start + 1
        duration = max(1, end - start)
        placements.append(ClipPlacement(record_frame=start, duration=duration, text=cue.text, start=cue.start, end=cue.end))
        previous_start = start
    # Second pass: a clip must not run into the next one.
    trimmed: "list[ClipPlacement]" = []
    for index, clip in enumerate(placements):
        if index + 1 < len(placements):
            limit = placements[index + 1].record_frame
            if clip.end_frame > limit:
                clip = ClipPlacement(clip.record_frame, max(1, limit - clip.record_frame), clip.text, clip.start, clip.end)
        trimmed.append(clip)
    return trimmed


def plan_tracks(
    transcript: Transcript,
    names: "dict[str, str]",
    mode: str,
    clock: TimelineClock,
    origin_frame: "int | None" = None,
    existing_track_names: "list[str] | None" = None,
    templates: "dict[str, object] | None" = None,
    srt_paths: "dict[str, str] | None" = None,
) -> "list[TrackPlan]":
    """One TrackPlan per speaker that has cues, in transcript speaker order.

    ``names`` maps speaker id to the (possibly user-edited) display name; missing ids fall back
    to the transcript's name, then the id. Track names are made unique against
    ``existing_track_names`` and against each other, so existing tracks are never touched.
    """
    if mode not in (MODE_SUBTITLES, MODE_TEXTPLUS):
        raise ValueError("unknown mode %r" % (mode,))
    taken = list(existing_track_names or [])
    plans: "list[TrackPlan]" = []
    for speaker_id in transcript.speaker_ids_with_cues():
        speaker = transcript.speaker(speaker_id)
        raw = names.get(speaker_id) or (speaker.display_name if speaker else speaker_id)
        name = unique_name(clean_track_name(raw, fallback=speaker_id), taken)
        taken.append(name)
        plan = TrackPlan(
            speaker_id=speaker_id,
            name=name,
            mode=mode,
            template=(templates or {}).get(speaker_id),
            srt_path=(srt_paths or {}).get(speaker_id),
        )
        # Subtitle mode uses the clips only to verify where Resolve put the SRT's cues.
        plan.clips = place_cues(transcript.cues_for(speaker_id), clock, origin_frame)
        plans.append(plan)
    return plans
