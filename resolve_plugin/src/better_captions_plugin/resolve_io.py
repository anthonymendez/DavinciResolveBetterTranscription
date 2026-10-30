"""Everything that touches the Resolve object model (see docs/RESOLVE_INTEGRATION.md for the calls and sources).

Keep this thin: decisions live in ``planning``/``timing`` and are tested against a fake
object model in ``tests/resolve/fake_resolve.py``; this module only sequences API calls
and verifies their effect.
"""

from __future__ import annotations

import glob
import os
import time
from dataclasses import dataclass, field, replace

from better_captions_plugin import compat, word_highlight
from better_captions_plugin.planning import TrackPlan
from better_captions_plugin.timing import TimelineClock
from better_captions_plugin.word_highlight import HighlightUnsupported


class ResolveError(Exception):
    pass


class SubtitlePlacementError(ResolveError):
    """ImportMedia/AppendToTimeline did not put the SRT where asked; nothing was left behind."""


@dataclass
class Template:
    name: str
    bin_path: str  # "Master/Templates" style path for the dialog
    item: object  # MediaPoolItem

    @property
    def label(self) -> str:
        return "%s  (%s)" % (self.name, self.bin_path)


@dataclass
class TrackResult:
    plan: TrackPlan
    track_type: str
    track_index: int
    placed: int = 0
    misplaced: list[str] = field(default_factory=list)  # human-readable notes
    text_failures: list[str] = field(default_factory=list)
    highlighted: int = 0  # clips whose spoken word got the highlight colour
    highlight_failures: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Context


def current_project_and_timeline(resolve, info=None):
    if resolve is None:
        raise ResolveError("no Resolve object: run this from Workspace > Scripts inside DaVinci Resolve")
    info = info or compat.detect(resolve)
    # A free Resolve that won't even report its version is most likely 21.1+ handing Python a
    # dead API object (compat.py): its "no project" means "needs Studio", not "open a project".
    api_dead = not info.studio and not info.version
    manager = resolve.GetProjectManager()
    if not manager:
        if api_dead:
            raise ResolveError(compat.NO_RESOLVE_MESSAGE)
        raise ResolveError("Resolve did not return its project manager; restart Resolve and try again")
    project = manager.GetCurrentProject()
    if not project:
        if api_dead:
            raise ResolveError(
                "open a project first (if one is open, Resolve is not letting this script see it).\n\n"
                + compat.NO_RESOLVE_MESSAGE
            )
        raise ResolveError("open a project first")
    timeline = project.GetCurrentTimeline()
    if not timeline:
        raise ResolveError("open a timeline first (the current timeline is captioned)")
    return project, timeline


def is_studio(resolve) -> bool:
    return compat.detect(resolve).studio


def timeline_clock(timeline, project=None) -> TimelineClock:
    frame_rate = _setting(timeline, "timelineFrameRate")
    if not frame_rate and project is not None:
        frame_rate = _setting(project, "timelineFrameRate")
    if not frame_rate:
        raise ResolveError("could not read the timeline frame rate")
    start_tc = None
    try:
        start_tc = timeline.GetStartTimecode()
    except Exception:
        pass
    start_frame = None
    try:
        start_frame = int(timeline.GetStartFrame())
    except Exception:
        pass
    drop_setting = _setting(timeline, "timelineDropFrameTimecode")
    if not drop_setting and project is not None:
        drop_setting = _setting(project, "timelineDropFrameTimecode")
    return TimelineClock.from_settings(
        frame_rate, start_timecode=start_tc, start_frame=start_frame, drop_frame_setting=drop_setting
    )


def _setting(obj, key):
    try:
        value = obj.GetSetting(key)
    except Exception:
        return None
    return value if value not in ("", None) else None


def render_range(timeline, clock: TimelineClock) -> tuple[int, int | None]:
    """``(start_frame, end_frame)`` of the in/out range if one is set, else ``(timeline start, None)``."""
    marks = None
    try:
        marks = timeline.GetMarkInOut()
    except Exception:
        marks = None
    if isinstance(marks, dict):
        for key in ("video", "audio"):
            section = marks.get(key)
            if isinstance(section, dict) and "in" in section and "out" in section:
                start, end = int(section["in"]), int(section["out"])
                if end >= start:
                    return start, end
    return clock.start_frame, None


def existing_track_names(timeline, track_type: str) -> list[str]:
    names = []
    try:
        count = int(timeline.GetTrackCount(track_type))
    except Exception:
        return names
    for index in range(1, count + 1):
        try:
            names.append(str(timeline.GetTrackName(track_type, index)))
        except Exception:
            names.append("")
    return names


# ---------------------------------------------------------------------------
# Audio render


def render_audio(
    project,
    timeline,
    out_dir: str,
    start_frame: int,
    end_frame: int | None,
    pump=None,
    should_cancel=None,
    timeout: float = 6 * 3600,
    sleep=time.sleep,
) -> str:
    """Render the timeline's audio (whole, or ``start_frame``..``end_frame``) as a WAV; return its path."""
    fmt, codec = pick_wav_format(project)
    previous_mode = None
    try:
        previous_mode = project.GetCurrentRenderMode()
    except Exception:
        pass
    if not project.SetCurrentRenderFormatAndCodec(fmt, codec):
        raise ResolveError("could not select the %s / %s render format" % (fmt, codec))
    try:
        project.SetCurrentRenderMode(1)  # single clip
    except Exception:
        pass
    os.makedirs(out_dir, exist_ok=True)
    name = "better-captions-%d" % int(time.time())
    settings = {
        "TargetDir": out_dir,
        "CustomName": name,
        "ExportVideo": False,
        "ExportAudio": True,
        "SelectAllFrames": end_frame is None,
    }
    if end_frame is not None:
        settings["MarkIn"] = int(start_frame)
        settings["MarkOut"] = int(end_frame)
    if not project.SetRenderSettings(settings):
        raise ResolveError("Resolve rejected the audio render settings")
    job = project.AddRenderJob()
    if not job:
        raise ResolveError("could not add the audio render job")
    try:
        if not project.StartRendering([job]):
            raise ResolveError("could not start the audio render")
        deadline = time.time() + timeout
        while project.IsRenderingInProgress():
            if pump:
                pump()
            if should_cancel and should_cancel():
                try:
                    project.StopRendering()
                except Exception:
                    pass
                raise ResolveError("cancelled")
            if time.time() > deadline:
                raise ResolveError("the audio render did not finish in time")
            sleep(0.2)
        status = {}
        try:
            status = project.GetRenderJobStatus(job) or {}
        except Exception:
            pass
        state = str(status.get("JobStatus", "")).lower()
        if state and state not in ("complete", "completed", "done"):
            raise ResolveError("the audio render ended with status %r" % status.get("JobStatus"))
    finally:
        try:
            project.DeleteRenderJob(job)
        except Exception:
            pass
        if previous_mode is not None:
            try:
                project.SetCurrentRenderMode(previous_mode)
            except Exception:
                pass
    candidates = [p for p in glob.glob(os.path.join(out_dir, "*.wav")) if name in os.path.basename(p)]
    candidates = candidates or glob.glob(os.path.join(out_dir, "*.wav"))
    if not candidates:
        raise ResolveError("the render finished but no WAV appeared in %s" % out_dir)
    return max(candidates, key=os.path.getmtime)


def pick_wav_format(project) -> tuple[str, str]:
    formats = project.GetRenderFormats() or {}
    fmt = None
    for key, ext in formats.items():
        if str(ext).lower().lstrip(".") == "wav":
            fmt = key
            break
    if fmt is None:
        for key in formats:
            if "wav" in str(key).lower():
                fmt = key
                break
    if fmt is None:
        raise ResolveError("this Resolve offers no WAV render format (formats: %s)" % ", ".join(map(str, formats)))
    codecs = project.GetRenderCodecs(fmt) or {}
    codec = None
    for description, value in codecs.items():
        if "pcm" in str(description).lower() or "pcm" in str(value).lower():
            codec = value
            break
    if codec is None and codecs:
        codec = next(iter(codecs.values()))
    if codec is None:
        raise ResolveError("no audio codec available for the %s render format" % fmt)
    return fmt, codec


# ---------------------------------------------------------------------------
# Media pool


def iter_media_pool(folder, path=()):
    """Yield ``(item, bin_path)`` for every clip under ``folder`` (recursively, depth first)."""
    name = _name(folder)
    here = (*path, name)
    for clip in folder.GetClipList() or []:
        yield clip, "/".join(here)
    for sub in folder.GetSubFolderList() or []:
        yield from iter_media_pool(sub, here)


def find_text_templates(media_pool) -> list[Template]:
    """Fusion titles / generators anywhere in the media pool: items without a file path.

    ``GetClipProperty("Type")`` is localised, so the type string is only used to exclude
    timelines and compound/multicam clips. Whether a candidate actually contains a Text+
    node is only known once it is on the timeline (``FindToolByID("TextPlus")``).
    """
    templates = []
    root = media_pool.GetRootFolder()
    if not root:
        return templates
    for item, bin_path in iter_media_pool(root):
        if looks_like_title_template(item):
            templates.append(Template(name=_name(item), bin_path=bin_path, item=item))
    return templates


def looks_like_title_template(item) -> bool:
    file_path = _clip_property(item, "File Path")
    if file_path:
        return False
    kind = str(_clip_property(item, "Type") or "").lower()
    return all(excluded not in kind for excluded in ("timeline", "compound", "multicam", "multi-cam", "stereo"))


def _clip_property(item, key):
    try:
        value = item.GetClipProperty(key)
    except Exception:
        return None
    if isinstance(value, dict):  # some versions return everything regardless of the key
        value = value.get(key)
    return value


def _name(obj) -> str:
    try:
        return str(obj.GetName())
    except Exception:
        return "?"


# ---------------------------------------------------------------------------
# Track creation (always new tracks; existing ones are never modified)


def add_track(timeline, track_type: str, name: str) -> int:
    before = int(timeline.GetTrackCount(track_type))
    if not timeline.AddTrack(track_type):
        raise ResolveError("Resolve refused to add a %s track" % track_type)
    after = int(timeline.GetTrackCount(track_type))
    if after != before + 1:
        raise ResolveError("adding a %s track did not change the track count (%d -> %d)" % (track_type, before, after))
    if not timeline.SetTrackName(track_type, after, name):
        # Not fatal: the clips still land on the right track.
        pass
    return after


def delete_track(timeline, track_type: str, index: int) -> bool:
    try:
        return bool(timeline.DeleteTrack(track_type, index))
    except Exception:
        return False


@dataclass
class HighlightStyle:
    """How to light the spoken word: its colour and where the comps are edited on disk."""

    rgb: tuple[float, float, float]
    work_dir: str  # the run's temp dir; one file per clip, removed after the import
    _count: int = 0

    def next_path(self) -> str:
        self._count += 1
        return os.path.join(self.work_dir, "highlight-%d.comp" % self._count)


def create_textplus_track(
    media_pool, timeline, plan: TrackPlan, highlight: HighlightStyle | None = None
) -> TrackResult:
    """A new video track named after the speaker with one Text+ clip per cue.

    The template (a MediaPoolItem) is appended once per cue with ``recordFrame``/``trackIndex``;
    the text is then set on each clip's Fusion comp. If the template has no Text+ node the
    track is removed again and a ResolveError raised.

    With ``highlight`` and a plan with word steps, each cue becomes one clip per step (same
    template, the whole cue's text) and the step's word is coloured (``apply_word_highlight``).
    If that fails on the first clip tried (the template's text is driven by another tool, this
    Resolve can't export/import comps), the track is rebuilt with one plain clip per cue and
    the result says why.
    """
    template = plan.template
    item = template.item if isinstance(template, Template) else template
    if item is None:
        raise ResolveError("no Text+ template chosen for %s" % plan.name)
    if highlight is not None and plan.highlight and any(clip.steps for clip in plan.clips):
        try:
            return _fill_textplus_track(media_pool, timeline, plan, item, highlight)
        except HighlightUnsupported as exc:
            result = _fill_textplus_track(media_pool, timeline, plan, item, None)
            result.notes.append("words not highlighted: %s" % exc)
            return result
    return _fill_textplus_track(media_pool, timeline, plan, item, None)


def _fill_textplus_track(media_pool, timeline, plan: TrackPlan, item, highlight: HighlightStyle | None) -> TrackResult:
    if highlight is None:
        clips = [(clip, None) for clip in plan.clips]
    else:
        clips = []
        for clip in plan.clips:
            if not clip.steps:
                clips.append((clip, None))
                continue
            for step in clip.steps:
                clips.append(
                    (replace(clip, record_frame=step.record_frame, duration=step.duration, steps=()), step.word)
                )
    index = add_track(timeline, "video", plan.name)
    result = TrackResult(plan=plan, track_type="video", track_index=index)
    try:
        infos = [
            {
                "mediaPoolItem": item,
                "startFrame": 0,
                "endFrame": clip.duration - 1,
                "trackIndex": index,
                "recordFrame": clip.record_frame,
                "mediaType": 1,
            }
            for clip, _word in clips
        ]
        appended = []
        for start in range(0, len(infos), 100):  # keep each call modest
            batch = media_pool.AppendToTimeline(infos[start : start + 100]) or []
            appended.extend(batch)
        if len(appended) != len(clips):
            appended = _items_by_start(timeline, "video", index)
        placed_by_frame = {}
        for timeline_item in appended:
            try:
                placed_by_frame.setdefault(int(timeline_item.GetStart()), timeline_item)
            except Exception:
                continue
        to_highlight = []
        for clip, word in clips:
            timeline_item = placed_by_frame.get(clip.record_frame)
            if timeline_item is None:
                result.misplaced.append("%s: no clip at frame %d" % (_short(clip.text), clip.record_frame))
                continue
            result.placed += 1
            try:
                duration = int(timeline_item.GetDuration())
            except Exception:
                duration = clip.duration
            if duration != clip.duration:
                result.misplaced.append("%s: %d frames instead of %d" % (_short(clip.text), duration, clip.duration))
            try:
                set_textplus_text(timeline_item, clip.text)
            except ResolveError as exc:
                result.text_failures.append("%s: %s" % (_short(clip.text), exc))
                continue
            if word is not None:
                to_highlight.append((timeline_item, clip, word))
        if result.placed == 0 and clips:
            raise ResolveError("none of the %d caption clips for %s could be placed" % (len(clips), plan.name))
        if clips and len(result.text_failures) == result.placed:
            raise ResolveError("template %s has no Text+ node (%s)" % (_name(item), result.text_failures[0]))
        for timeline_item, clip, word in to_highlight:
            try:
                apply_word_highlight(timeline_item, clip.text, word, highlight)
                result.highlighted += 1
            except HighlightUnsupported as exc:
                if result.highlighted == 0:
                    raise  # nothing lit yet: the caller rebuilds the track without highlighting
                result.highlight_failures.append("%s (%s): %s" % (_short(clip.text), word.text, exc))
    except Exception:
        delete_track(timeline, "video", index)
        raise
    return result


def apply_word_highlight(timeline_item, text: str, word, style: HighlightStyle):
    """Colour ``word`` (a word_highlight.CueWord) of the clip's Text+ ``text``.

    ``SetInput`` can't write Character Level Styling (KathaCut's live spike, see
    docs/RESOLVE_INTEGRATION.md §8), but a comp file round trip can: export the clip's comp,
    add a ``StyledTextCLS`` tool feeding the Text+ ``StyledText`` (word_highlight), import it.
    Raises HighlightUnsupported on any failure; the clip keeps its plain text.
    """
    path = style.next_path()
    try:
        os.makedirs(style.work_dir, exist_ok=True)
        try:
            exported = timeline_item.ExportFusionComp(path, 1)
        except Exception as exc:
            raise HighlightUnsupported("Resolve could not export the clip's Fusion comp (%s)" % exc) from exc
        if not exported or not os.path.isfile(path):
            raise HighlightUnsupported("Resolve did not export the clip's Fusion comp")
        # surrogateescape: bytes that aren't UTF-8 go back out unchanged.
        with open(path, encoding="utf-8", errors="surrogateescape", newline="") as handle:
            comp_text = handle.read()
        entries = word_highlight.cls_entries(word.char_start, word.char_end, style.rgb)
        edited = word_highlight.add_highlight_to_comp(comp_text, text, entries)
        with open(path, "w", encoding="utf-8", errors="surrogateescape", newline="") as handle:
            handle.write(edited)
        try:
            comp = timeline_item.ImportFusionComp(path)
        except Exception as exc:
            raise HighlightUnsupported("Resolve could not import the highlighted comp (%s)" % exc) from exc
        if not comp:
            raise HighlightUnsupported("Resolve did not import the highlighted comp")
        _activate_last_comp(timeline_item)
    except OSError as exc:
        raise HighlightUnsupported("could not edit the comp file: %s" % exc) from exc
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def _activate_last_comp(timeline_item):
    """If the import added a comp instead of replacing the clip's, make the new one current."""
    try:
        if int(timeline_item.GetFusionCompCount()) <= 1:
            return
        names = timeline_item.GetFusionCompNameList() or []
    except Exception:
        return
    if isinstance(names, dict):  # a Lua table through the bridge: {1: ..., 2: ..., "__flags": ...}
        names = [names[key] for key in sorted(k for k in names if isinstance(k, int))]
    names = [name for name in names if isinstance(name, str)]
    if names:
        try:
            timeline_item.LoadFusionCompByName(names[-1])
        except Exception:
            pass


def set_textplus_text(timeline_item, text: str):
    comp = None
    try:
        if int(timeline_item.GetFusionCompCount()) >= 1:
            comp = timeline_item.GetFusionCompByIndex(1)
    except Exception:
        comp = None
    if not comp:
        raise ResolveError("clip has no Fusion composition")
    tool = None
    try:
        tool = comp.FindToolByID("TextPlus")
    except Exception:
        tool = None
    if not tool:
        try:
            tools = comp.GetToolList(False, "TextPlus") or {}
            tool = next(iter(tools.values()), None) if isinstance(tools, dict) else None
        except Exception:
            tool = None
    if not tool:
        raise ResolveError("no Text+ node in the composition")
    try:
        tool.SetInput("StyledText", text)
    except Exception as exc:
        raise ResolveError("could not set StyledText: %s" % exc) from exc


def create_subtitle_track(media_pool, timeline, plan: TrackPlan, origin_frame: int) -> TrackResult:
    """A new subtitle track named after the speaker, filled from the speaker's SRT.

    Resolve has no dedicated API for this: the SRT is imported into the media pool and
    appended to the new subtitle track at ``origin_frame`` (where the rendered audio starts).
    The result is verified; on any mismatch everything created here is removed and a
    SubtitlePlacementError raised so the caller can fall back to manual import.
    """
    if not plan.srt_path or not os.path.isfile(plan.srt_path):
        raise SubtitlePlacementError("no SRT file for %s" % plan.name)
    imported = []
    try:
        imported = media_pool.ImportMedia([plan.srt_path]) or []
    except Exception as exc:
        raise SubtitlePlacementError("ImportMedia failed for %s: %s" % (plan.srt_path, exc)) from exc
    if not imported:
        raise SubtitlePlacementError("Resolve did not import %s into the media pool" % plan.srt_path)
    srt_item = imported[0]
    index = None
    try:
        index = add_track(timeline, "subtitle", plan.name)
        info = {"mediaPoolItem": srt_item, "trackIndex": index, "recordFrame": int(origin_frame)}
        media_pool.AppendToTimeline([info])
        items = _items_by_start(timeline, "subtitle", index)
        if not items:
            raise SubtitlePlacementError("no subtitle clips appeared on the new track")
        expected_first = plan.first_frame if plan.first_frame is not None else origin_frame
        first = int(items[0].GetStart())
        if first != expected_first:
            raise SubtitlePlacementError(
                "subtitles landed at frame %d instead of %d (a known Resolve API issue when the "
                "video track already has clips)" % (first, expected_first)
            )
        if plan.clips and len(items) != len(plan.clips):
            raise SubtitlePlacementError("%d subtitle clips appeared, expected %d" % (len(items), len(plan.clips)))
        return TrackResult(plan=plan, track_type="subtitle", track_index=index, placed=len(items))
    except Exception as exc:
        if index is not None:
            delete_track(timeline, "subtitle", index)
        try:
            media_pool.DeleteClips([srt_item])
        except Exception:
            pass
        if isinstance(exc, SubtitlePlacementError):
            raise
        raise SubtitlePlacementError(str(exc)) from exc


def _items_by_start(timeline, track_type: str, index: int) -> list:
    items = []
    try:
        items = list(timeline.GetItemListInTrack(track_type, index) or [])
    except Exception:
        return []

    def start(item):
        try:
            return int(item.GetStart())
        except Exception:
            return 0

    return sorted(items, key=start)


def _short(text: str, limit: int = 40) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"
