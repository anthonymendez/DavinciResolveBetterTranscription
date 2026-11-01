"""A fake of the slice of Resolve's scripting object model the plugin uses.

Method names, argument shapes and return conventions follow Blackmagic's scripting README
(see docs/RESOLVE_INTEGRATION.md). Knobs on the fakes reproduce the behaviours the plugin
has to cope with: a subtitle append that ignores recordFrame (and on 21.1.2 trackIndex too), a template without a Text+
node, a render that needs several polls, AppendToTimeline returning fewer items, ...

``PROFILES`` describes the Resolve releases the plugin has to run on (product name, version,
and which of those knobs each one needs); ``make_world(profile="21.1.2-studio")`` builds a
world for one of them.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from better_captions_plugin.timing import frames_to_timecode, timecode_to_frames

_SRT_TIME = re.compile(r"(\d+):(\d\d):(\d\d)[,.](\d{3})")


def _srt_seconds(text: str) -> float:
    h, m, s, ms = _SRT_TIME.match(text).groups()
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def parse_srt(path: str) -> list[tuple[float, float, str]]:
    cues = []
    with open(path, encoding="utf-8") as handle:
        blocks = re.split(r"\n\s*\n", handle.read().strip())
    for block in blocks:
        lines = block.strip().splitlines()
        if len(lines) < 2:
            continue
        times = lines[1] if "-->" in lines[1] else lines[0]
        start, end = [t.strip() for t in times.split("-->")]
        text = "\n".join(lines[2:] if "-->" in lines[1] else lines[1:])
        cues.append((_srt_seconds(start), _srt_seconds(end), text))
    return cues


class FakeTool:
    def __init__(self, tool_id: str, name: str):
        self.id = tool_id
        self.name = name
        self.inputs = {}

    def SetInput(self, key, value, time=None):
        self.inputs[key] = value
        return True

    def GetInput(self, key, time=None):
        return self.inputs.get(key)

    def GetAttrs(self):
        return {"TOOLS_Name": self.name, "TOOLS_RegID": self.id}


def _lua_quote(text: str) -> str:
    return '"%s"' % str(text).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _lua_unquote(body: str) -> str:
    return re.sub(r"\\(.)", lambda m: {"n": "\n", "t": "\t", "r": "\r"}.get(m.group(1), m.group(1)), body)


_LUA_STRING = r'"((?:[^"\\\n]|\\.)*)"'


class FakeComp:
    def __init__(self, tools=None, name="Composition 1", styled_text_source=None):
        self.tools = list(tools or [])
        self.name = name
        self.styled_text_source = styled_text_source  # a template whose StyledText another tool drives
        self.cls = None  # after an import: {"tool", "text", "array": [(id, first, last, value)]}

    def FindToolByID(self, tool_id):
        for tool in self.tools:
            if tool.id == tool_id:
                return tool
        return None

    def GetToolList(self, selected=False, tool_id=None):
        return {i + 1: t for i, t in enumerate(self.tools) if tool_id is None or t.id == tool_id}

    def comp_text(self, duration: int) -> str:
        """What ExportFusionComp writes, shaped like a Resolve 21 Text+ title's comp."""
        tool = self.FindToolByID("TextPlus")
        if tool is None:
            body = (
                "\t\tBackground1 = Background {\n\t\t\tInputs = {\n"
                "\t\t\t\tWidth = Input { Value = 1920, },\n\t\t\t},\n\t\t},\n"
            )
        else:
            text = tool.GetInput("StyledText") or ""
            follower = ""
            if self.styled_text_source:
                styled = (
                    'StyledText = Input {\n\t\t\t\t\tSourceOp = "%s",\n\t\t\t\t\tSource = "StyledText",\n\t\t\t\t},'
                    % self.styled_text_source
                )
                follower = (
                    "\t\t%s = StyledTextFollower {\n\t\t\tInputs = {\n\t\t\t\tText = Input { Value = %s, },\n"
                    "\t\t\t},\n\t\t},\n" % (self.styled_text_source, _lua_quote(text))
                )
            else:
                styled = "StyledText = Input { Value = %s, }," % _lua_quote(text)
            body = (
                follower + "\t\t%s = TextPlus {\n" % tool.name + "\t\t\tInputs = {\n"
                "\t\t\t\tGlobalOut = Input { Value = %d, },\n"
                % (duration - 1)
                + "\t\t\t\tWidth = Input { Value = 1920, },\n"
                "\t\t\t\t%s\n" % styled + '\t\t\t\tFont = Input { Value = "Open Sans", },\n'
                '\t\t\t\tStyle = Input { Value = "Bold", },\n'
                "\t\t\t\tRed1 = Input { Value = 0.95, },\n"
                "\t\t\t},\n"
                "\t\t\tViewInfo = OperatorInfo { Pos = { 220, 49.5 } },\n"
                "\t\t},\n"
                "\t\tMediaOut1 = Saver {\n"
                "\t\t\tInputs = {\n"
                "\t\t\t\tInput = Input {\n"
                '\t\t\t\t\tSourceOp = "%s",\n' % tool.name + '\t\t\t\t\tSource = "Output",\n'
                "\t\t\t\t}\n"
                "\t\t\t},\n"
                "\t\t},\n"
            )
        return (
            "Composition {\n"
            "\tCurrentTime = 0,\n"
            "\tRenderRange = { 0, %d },\n" % (duration - 1) + '\tCustomData = {\n\t\tTEMPLATE_ID = "Text+"\n\t},\n'
            "\tTools = ordered() {\n%s\t},\n" % body + "\tPrefs = {\n\t\tComp = {\n\t\t\tPaths = {\n\t\t\t\tMap = {\n"
            '\t\t\t\t\t["ResolveCaches:"] = "C:\\\\Cache {v2} = TextPlus {",\n'  # braces inside a string
            "\t\t\t\t},\n\t\t\t},\n\t\t}\n\t},\n"
            "}\n"
        )


def parse_imported_comp(text: str) -> FakeComp | None:
    """What ImportFusionComp makes of a comp file: None unless it is well formed and its Text+ tool's
    StyledText comes from a StyledTextCLS tool (the only edit the plugin makes)."""
    code = re.sub(_LUA_STRING, '""', text)
    if code.count("{") != code.count("}") or not code.strip().startswith("Composition {"):
        return None
    tool = re.search(r"(\w+) = TextPlus \{", text)
    link = re.search(r'StyledText = Input \{\s*SourceOp = "(\w+)",\s*Source = "StyledText",\s*\}', text)
    if not tool or not link:
        return None
    cls = re.search(r"(?<![\w])%s = StyledTextCLS \{" % link.group(1), text)
    if not cls:
        return None
    block = text[cls.start() :]
    text_value = re.search(r"(?<![\w])Text = Input \{ Value = %s, \}" % _LUA_STRING, block)
    array = re.search(r"Array = \{(.*?)\n\s*\},", block, re.S)
    if not text_value or not array:
        return None
    entries = [
        (int(a), int(b), int(c), float(v))
        for a, b, c, v in re.findall(r"\{ (\d+), (\d+), (\d+), Value = ([\d.]+) \}", array.group(1))
    ]
    shown = _lua_unquote(text_value.group(1))
    comp = FakeComp([FakeTool("TextPlus", tool.group(1))])
    comp.tools[0].SetInput("StyledText", shown)
    comp.cls = {"tool": link.group(1), "text": shown, "array": entries}
    comp.source_text = text
    return comp


class FakeTimelineItem:
    _ids = 0

    def __init__(self, name: str, start: int, duration: int, comps=None, media_pool_item=None):
        self.name = name
        self.start = int(start)
        self.duration = int(duration)
        self.comps = list(comps or [])
        self.media_pool_item = media_pool_item
        self.color = None
        FakeTimelineItem._ids += 1
        self.unique_id = "item-%d" % FakeTimelineItem._ids
        self.timeline = None  # the FakeTimeline it was placed on
        self.active = 0  # index of the current comp
        # knobs
        self.comp_io = True  # False: ExportFusionComp fails (an older Resolve)
        self.import_fails = False
        self.import_replaces = False  # True: ImportFusionComp swaps the comp instead of adding one
        self.exported = []
        self.loaded = []

    def GetName(self):
        return self.name

    def GetStart(self):
        return self.start

    def GetEnd(self):
        return self.start + self.duration

    def GetDuration(self):
        return self.duration

    def GetUniqueId(self):
        return self.unique_id

    def GetPosition(self, subframePrecision=False):
        where = self.timeline._position(self) if self.timeline is not None else None
        return where or {"startFrame": self.start}

    def GetFusionCompCount(self):
        return len(self.comps)

    def GetFusionCompByIndex(self, index):
        return self.comps[index - 1] if 1 <= index <= len(self.comps) else None

    def GetFusionCompNameList(self):  # a Lua table through the bridge, as KathaCut's spike logged it
        names = {i + 1: comp.name for i, comp in enumerate(self.comps)}
        names["__flags"] = 4194304
        return names

    def LoadFusionCompByName(self, name):
        for index, comp in enumerate(self.comps):
            if comp.name == name:
                self.active = index
                self.loaded.append(name)
                return comp
        return None

    def ExportFusionComp(self, path, index):
        if not self.comp_io or not 1 <= index <= len(self.comps):
            return False
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(self.comps[index - 1].comp_text(self.duration))
        self.exported.append(path)
        return True

    def ImportFusionComp(self, path):
        if self.import_fails or not os.path.isfile(path):
            return None
        with open(path, encoding="utf-8") as handle:
            comp = parse_imported_comp(handle.read())
        if comp is None:
            return None
        if self.import_replaces:
            comp.name = self.comps[self.active].name
            self.comps[self.active] = comp
        else:
            comp.name = "Composition %d" % (len(self.comps) + 1)
            self.comps.append(comp)
        return comp

    def SetClipColor(self, color):
        self.color = color
        return True

    def text(self):
        """Test helper: the StyledText of the current comp's Text+ node."""
        for comp in self.comps[self.active :] + self.comps[: self.active]:
            tool = comp.FindToolByID("TextPlus")
            if tool:
                return tool.GetInput("StyledText")
        return None

    def highlight(self):
        """Test helper: the current comp's character styling (``FakeComp.cls``), or None."""
        return self.comps[self.active].cls if self.comps else None


class FakeMediaPoolItem:
    def __init__(
        self,
        name: str,
        kind: str = "Video + Audio",
        file_path: str | None = None,
        has_textplus: bool = True,
        srt_cues=None,
        placed_duration=None,
        styled_text_source=None,
    ):
        self.name = name
        self.props = {"Clip Name": name, "Type": kind, "File Path": "" if file_path is None else file_path}
        self.has_textplus = has_textplus
        self.srt_cues = srt_cues
        self.placed_duration = placed_duration  # if set, AppendToTimeline ignores endFrame (template quirk)
        self.styled_text_source = styled_text_source  # e.g. "Follower1": the template animates its own text

    def GetName(self):
        return self.name

    def GetClipProperty(self, key=None):
        if key is None:
            return dict(self.props)
        return self.props.get(key, "")

    def is_srt(self):
        return self.srt_cues is not None


class FakeFolder:
    def __init__(self, name: str, clips=None, subfolders=None):
        self.name = name
        self.clips = list(clips or [])
        self.subfolders = list(subfolders or [])

    def GetName(self):
        return self.name

    def GetClipList(self):
        return list(self.clips)

    def GetSubFolderList(self):
        return list(self.subfolders)


@dataclass
class FakeTrack:
    name: str
    items: list[FakeTimelineItem] = field(default_factory=list)
    enabled: bool = True


class FakeTimeline:
    def __init__(
        self,
        name="Timeline 1",
        frame_rate="24",
        start_timecode="01:00:00:00",
        drop_frame=False,
        mark_in_out=None,
        has_mark_in_out_api=True,
        has_track_enable_api=True,
        has_move_items_api=False,
    ):
        self.name = name
        self.settings = {"timelineFrameRate": frame_rate, "timelineDropFrameTimecode": "1" if drop_frame else "0"}
        fps = float(str(frame_rate).replace("DF", "").strip())
        self.start_timecode = start_timecode
        self.start_frame = timecode_to_frames(start_timecode, fps, drop_frame)
        self.fps = fps
        self.drop_frame = drop_frame
        self.tracks = {"video": [FakeTrack("Video 1")], "audio": [FakeTrack("Audio 1")], "subtitle": []}
        self.mark_in_out = mark_in_out
        self.has_mark_in_out_api = has_mark_in_out_api
        self.has_track_enable_api = has_track_enable_api  # False: GetIsTrackEnabled/SetTrackEnable don't exist
        self.has_move_items_api = has_move_items_api  # True: Timeline.MoveItems (21.1.x)
        # knobs
        self.refuse_track_enable = ()  # (track_type, index) pairs SetTrackEnable returns False for
        self.enabled_reads_stale = False  # True: GetIsTrackEnabled always says True, as Resolve 20 can
        self.track_enable_calls = []  # every accepted or refused SetTrackEnable(track_type, index, enabled)
        self.refuse_add_track = False
        self.subtitle_append_ignores_record_frame = False  # True: the cues start after the last video clip
        # Resolve 21.1.2: an SRT append ignores recordFrame and trackIndex; the cues go onto Subtitle 1,
        # starting at the end of the timeline (every track) plus the SRT's own offsets
        self.subtitle_append_at_timeline_end = False
        self.subtitle_drops_empty_cues = False  # 21.1.2: a cue whose SRT start and end are equal is not placed
        self.refuse_move_items = False
        self.deleted_tracks = []
        self.current_timecode = start_timecode

    # -- settings / identity
    def GetName(self):
        return self.name

    def GetSetting(self, key):
        return self.settings.get(key, "")

    def GetStartFrame(self):
        return self.start_frame

    def GetEndFrame(self):
        end = self.start_frame
        for tracks in self.tracks.values():
            for track in tracks:
                for item in track.items:
                    end = max(end, item.GetEnd())
        return end

    def GetStartTimecode(self):
        return self.start_timecode

    def GetCurrentTimecode(self):
        return self.current_timecode

    def GetMarkInOut(self):
        if not self.has_mark_in_out_api:
            raise AttributeError("GetMarkInOut")
        return dict(self.mark_in_out or {})

    def SetMarkInOut(self, mark_in, mark_out, mark_type="all"):
        if not self.has_mark_in_out_api:
            raise AttributeError("SetMarkInOut")
        marks = dict(self.mark_in_out or {})
        for key in ("video", "audio") if mark_type == "all" else (mark_type,):
            marks[key] = {"in": int(mark_in), "out": int(mark_out)}
        self.mark_in_out = marks
        return True

    def DeleteClips(self, items, ripple=False):
        for track in (t for tracks in self.tracks.values() for t in tracks):
            track.items = [i for i in track.items if i not in items]
        return True

    # -- tracks
    def GetTrackCount(self, track_type):
        return len(self.tracks[track_type])

    def AddTrack(self, track_type, options=None):
        if self.refuse_add_track:
            return False
        number = len(self.tracks[track_type]) + 1
        self.tracks[track_type].append(FakeTrack("%s %d" % (track_type.capitalize(), number)))
        return True

    def DeleteTrack(self, track_type, index):
        if not 1 <= index <= len(self.tracks[track_type]):
            return False
        self.deleted_tracks.append((track_type, index, self.tracks[track_type][index - 1].name))
        del self.tracks[track_type][index - 1]
        return True

    def SetTrackName(self, track_type, index, name):
        if not 1 <= index <= len(self.tracks[track_type]):
            return False
        self.tracks[track_type][index - 1].name = name
        return True

    def GetTrackName(self, track_type, index):
        return self.tracks[track_type][index - 1].name

    def GetItemListInTrack(self, track_type, index):
        if not 1 <= index <= len(self.tracks[track_type]):
            return []
        return list(self.tracks[track_type][index - 1].items)

    def __getattr__(self, name):
        if name in ("GetIsTrackEnabled", "SetTrackEnable") and self.has_track_enable_api:
            return getattr(self, "_" + name)
        if name == "MoveItems" and self.has_move_items_api:
            return self._MoveItems
        raise AttributeError(name)

    def _GetIsTrackEnabled(self, track_type, index):
        if not 1 <= index <= len(self.tracks[track_type]):
            return False
        return self.enabled_reads_stale or self.tracks[track_type][index - 1].enabled

    def _SetTrackEnable(self, track_type, index, enabled):
        self.track_enable_calls.append((track_type, index, bool(enabled)))
        if (track_type, index) in self.refuse_track_enable or not 1 <= index <= len(self.tracks[track_type]):
            return False
        self.tracks[track_type][index - 1].enabled = bool(enabled)
        return True

    def _MoveItems(self, items, position, moveLinkedItems=True, subframePrecision=False):
        """Move the leftmost item to ``position`` and the others by the same offset (one track type)."""
        if self.refuse_move_items or not items:
            return False
        located = [(item, self._position(item)) for item in items]
        if any(where is None for _, where in located):
            return False
        _, lead = min(located, key=lambda pair: (pair[1]["startFrame"], pair[1]["trackIndex"]))
        frame_shift = int(position.get("startFrame", lead["startFrame"])) - lead["startFrame"]
        track_shift = int(position.get("trackIndex", lead["trackIndex"])) - lead["trackIndex"]
        moves = []
        for item, where in located:
            tracks = self.tracks[where["trackType"]]
            target = where["trackIndex"] + track_shift
            if not 1 <= target <= len(tracks):
                return False
            moves.append((item, tracks[target - 1], item.start + frame_shift))
        for item, track, start in moves:
            for tracks in self.tracks.values():
                for old in tracks:
                    if item in old.items:
                        old.items.remove(item)
            item.start = start
            track.items.append(item)
            track.items.sort(key=lambda c: c.start)
        return True

    def _position(self, item):
        for track_type, tracks in self.tracks.items():
            for index, track in enumerate(tracks, start=1):
                if item in track.items:
                    return {"trackType": track_type, "trackIndex": index, "startFrame": item.start}
        return None

    def enabled_audio(self) -> list[int]:
        """Test helper: the 1-based indices of the enabled audio tracks."""
        return [i for i, track in enumerate(self.tracks["audio"], start=1) if track.enabled]

    def track(self, track_type, name) -> FakeTrack | None:
        return next((t for t in self.tracks[track_type] if t.name == name), None)

    def _video_content_end(self):
        end = self.start_frame
        for track in self.tracks["video"]:
            for item in track.items:
                end = max(end, item.GetEnd())
        return end


class FakeMediaPool:
    def __init__(self, timeline: FakeTimeline, root: FakeFolder | None = None):
        self.timeline = timeline
        self.root = root or FakeFolder("Master")
        self.imported = []
        self.deleted = []
        self.append_calls = []
        self.append_returns_items = True
        # Resolve 21.1: endFrame is exclusive, a clip with endFrame == startFrame fails the whole call
        # (None for every clip), and appending clears the timeline's mark in/out
        self.end_frame_exclusive = False
        self.append_clears_marks = False

    def GetRootFolder(self):
        return self.root

    def GetCurrentFolder(self):
        return self.root

    def ImportMedia(self, paths):
        items = []
        for path in paths:
            if not os.path.isfile(path):
                continue
            if path.lower().endswith(".srt"):
                item = FakeMediaPoolItem(
                    os.path.basename(path), kind="Subtitle", file_path=path, srt_cues=parse_srt(path)
                )
            else:
                item = FakeMediaPoolItem(os.path.basename(path), file_path=path)
            self.root.clips.append(item)
            self.imported.append(item)
            items.append(item)
        return items

    def DeleteClips(self, items):
        for item in items:
            if item in self.root.clips:
                self.root.clips.remove(item)
            self.deleted.append(item)
        return True

    def AppendToTimeline(self, clips):
        self.append_calls.append(list(clips))
        if self.append_clears_marks:
            self.timeline.mark_in_out = None
        if self.end_frame_exclusive and any(
            isinstance(i, dict) and "endFrame" in i and int(i["endFrame"]) <= int(i.get("startFrame", 0)) for i in clips
        ):
            return None
        placed = []
        for info in clips:
            if not isinstance(info, dict):
                info = {"mediaPoolItem": info}
            item = info["mediaPoolItem"]
            if item.is_srt():
                placed.extend(self._append_srt(item, info))
            else:
                placed.append(self._append_clip(item, info))
        return placed if self.append_returns_items else []

    def _append_srt(self, item, info):
        timeline = self.timeline
        index = int(info.get("trackIndex", max(1, len(timeline.tracks["subtitle"]))))
        if not timeline.tracks["subtitle"]:
            timeline.AddTrack("subtitle")
        origin = int(info.get("recordFrame", timeline.start_frame))
        if timeline.subtitle_append_at_timeline_end:
            index, origin = 1, timeline.GetEndFrame()
        elif timeline.subtitle_append_ignores_record_frame:
            origin = timeline._video_content_end()
        track = timeline.tracks["subtitle"][index - 1]
        items = []
        for start, end, text in item.srt_cues:
            if timeline.subtitle_drops_empty_cues and end <= start:
                continue
            frame = origin + int(round(start * timeline.fps))
            duration = max(1, int(round(end * timeline.fps)) - int(round(start * timeline.fps)))
            clip = FakeTimelineItem(text, frame, duration, media_pool_item=item)
            clip.timeline = timeline
            track.items.append(clip)
            items.append(clip)
        track.items.sort(key=lambda c: c.start)
        return items

    def _append_clip(self, item, info):
        timeline = self.timeline
        index = int(info.get("trackIndex", 1))
        while len(timeline.tracks["video"]) < index:
            timeline.AddTrack("video")
        track = timeline.tracks["video"][index - 1]
        start_frame = int(info.get("startFrame", 0))
        end_frame = int(info.get("endFrame", start_frame + 119))
        duration = end_frame - start_frame + (0 if self.end_frame_exclusive else 1)
        template_fps = item.props.get("FPS")
        if template_fps:  # start/end count in the template's own frames; Resolve rounds the length down
            duration = int(duration * timeline.fps / float(template_fps) + 1e-9)
        if item.placed_duration is not None:
            duration = item.placed_duration
        record = int(info.get("recordFrame", track.items[-1].GetEnd() if track.items else timeline.start_frame))
        comps = [
            FakeComp(
                [FakeTool("TextPlus", "Template")] if item.has_textplus else [FakeTool("Background", "Background1")],
                styled_text_source=item.styled_text_source,
            )
        ]
        clip = FakeTimelineItem(item.name, record, duration, comps=comps, media_pool_item=item)
        clip.timeline = timeline
        track.items.append(clip)
        track.items.sort(key=lambda c: c.start)
        return clip


class FakeProject:
    def __init__(self, timeline: FakeTimeline, media_pool: FakeMediaPool | None = None, name="Project"):
        self.name = name
        self.timeline = timeline
        self.media_pool = media_pool or FakeMediaPool(timeline)
        # As measured on real Resolve (docs/RESOLVE_INTEGRATION.md §5): formats map name -> extension,
        # the codec calls take the extension, and Wave exposes no codecs, so no ("wav", codec) is accepted.
        self.formats = {"QuickTime": "mov", "MP4": "mp4", "Wave": "wav"}
        self.codecs = {"mov": {"H.264": "H264", "Apple ProRes 422": "ProRes422"}, "mp4": {"H.264": "H264"}}
        self.presets = ["Audio Only", "H.264 Master", "YouTube - 1080p"]  # stock presets ("Audio Only" is localised)
        # 21.1+: GetAudioRenderFormats / GetAudioRenderCodecs and the "AudioFormat" render setting.
        self.audio_render_api = False
        self.render_format = ("mov", "H264")
        self.render_mode = 0
        self.render_settings = {}
        self.render_sets_relative_marks = False  # True: MarkIn/MarkOut overwrite the timeline's marks (21.1)
        self.settings_calls = []  # every SetRenderSettings dict, accepted or not
        self.start_failures = 0  # StartRendering returns False this many times first (seen on 21.1)
        self.stale_range_blocks_select_all = False
        self.last_job = None  # the most recent job as added, kept after DeleteRenderJob
        self.jobs = {}
        self.polls_until_done = 2
        self._rendering = 0
        self.deleted_jobs = []
        self.fail_render = False
        self.settings = {"timelineFrameRate": timeline.settings["timelineFrameRate"]}

    def GetName(self):
        return self.name

    def GetSetting(self, key):
        return self.settings.get(key, "")

    def GetCurrentTimeline(self):
        return self.timeline

    def GetMediaPool(self):
        return self.media_pool

    # -- render
    def GetRenderFormats(self):
        return dict(self.formats)

    def GetRenderCodecs(self, ext):
        return dict(self.codecs.get(ext, {}))

    def SetCurrentRenderFormatAndCodec(self, ext, codec):
        if ext not in self.formats.values() or codec not in self.codecs.get(ext, {}).values():
            return False
        self.render_format = (ext, codec)
        return True

    def GetCurrentRenderFormatAndCodec(self):
        return {"format": self.render_format[0], "codec": self.render_format[1]}

    def GetRenderPresetList(self):
        return list(self.presets)

    def LoadRenderPreset(self, name):
        if name not in self.presets:
            return False
        if "audio" in name.lower():
            self.render_format = ("wav", "")
            self.render_settings.update({"ExportVideo": False, "ExportAudio": True})
        else:
            self.render_format = ("mp4", "H264")
            self.render_settings.update({"ExportVideo": True, "ExportAudio": True})
        return True

    def __getattr__(self, name):
        if name in ("GetAudioRenderFormats", "GetAudioRenderCodecs") and self.audio_render_api:
            if name == "GetAudioRenderFormats":
                return lambda: {"Wave": "wav", "QuickTime": "mov"}
            return lambda ext: {"Linear PCM": "lpcm"} if ext == "wav" else {"AAC": "aac"}
        raise AttributeError(name)

    def GetCurrentRenderMode(self):
        return self.render_mode

    def SetCurrentRenderMode(self, mode):
        self.render_mode = mode
        return True

    def SetRenderSettings(self, settings):
        self.settings_calls.append(dict(settings))
        if "AudioFormat" in settings and not self.audio_render_api:
            return False  # a key this build doesn't know
        self.render_settings.update(settings)  # layered on the current state, like Resolve
        if self.render_sets_relative_marks and "MarkIn" in settings and "MarkOut" in settings:
            start = self.timeline.start_frame  # 21.1: the job's range becomes the timeline's marks
            self.timeline.SetMarkInOut(int(settings["MarkIn"]) - start, int(settings["MarkOut"]) - start)
        return True

    def AddRenderJob(self):
        job_id = "job-%d" % (len(self.jobs) + 1)
        self.jobs[job_id] = {"settings": dict(self.render_settings), "format": self.render_format, "status": "Ready"}
        self.last_job = self.jobs[job_id]
        return job_id

    def StartRendering(self, job_ids=None, is_interactive=False):
        if self.start_failures > 0:
            self.start_failures -= 1
            return False
        # Real 21.x: after a MarkIn/MarkOut job, a SelectAllFrames-only job won't start.
        if self.stale_range_blocks_select_all and any(
            self.jobs[j]["settings"].get("SelectAllFrames") for j in (job_ids or self.jobs)
        ):
            return False
        self._rendering = self.polls_until_done
        self._active = list(job_ids or self.jobs)
        timeline = self.timeline
        enabled = timeline.enabled_audio() if timeline is not None and hasattr(timeline, "enabled_audio") else None
        for job_id in self._active:
            self.jobs[job_id]["status"] = "Rendering"
            self.jobs[job_id]["enabled_audio"] = enabled  # the audio tracks this render hears
        return True

    def IsRenderingInProgress(self):
        if self._rendering > 0:
            self._rendering -= 1
            if self._rendering == 0:
                self._finish()
            return True
        return False

    def _finish(self):
        for job_id in self._active:
            job = self.jobs[job_id]
            if self.fail_render:
                job["status"] = "Failed"
                continue
            settings = job["settings"]
            target = settings["TargetDir"]
            os.makedirs(target, exist_ok=True)
            ext = settings.get("AudioFormat") if not settings.get("ExportVideo") else None
            ext = ext or job["format"][0]
            name = "%s.%s" % (settings.get("CustomName", "render"), ext)
            with open(os.path.join(target, name), "wb") as handle:
                handle.write(b"RIFF....WAVEfake")
                if job.get("enabled_audio") is not None:  # tests tell a mix from a single-track render
                    handle.write((" tracks=%s" % ",".join(map(str, job["enabled_audio"]))).encode("ascii"))
            job["status"] = "Complete"

    def GetRenderJobStatus(self, job_id):
        job = self.jobs.get(job_id)
        return {"JobStatus": job["status"], "CompletionPercentage": 100} if job else {}

    def DeleteRenderJob(self, job_id):
        self.deleted_jobs.append(job_id)
        return self.jobs.pop(job_id, None) is not None

    def StopRendering(self):
        self._rendering = 0
        return True


class FakeProjectManager:
    def __init__(self, project):
        self.project = project

    def GetCurrentProject(self):
        return self.project


class FakeResolve:
    def __init__(self, project: FakeProject, product="DaVinci Resolve", version="20.0.0.0023", python_gated=False):
        self.project = project
        self.product = product
        self.version = version
        # knobs
        self.python_gated = python_gated  # free 21.1+: the object exists but GetProjectManager() gives None

    def GetProjectManager(self):
        if self.python_gated:
            return None
        return FakeProjectManager(self.project)

    def GetProductName(self):
        return self.product

    def GetVersion(self):
        """``[major, minor, patch, build, suffix]``, e.g. ``[21, 1, 2, 5, ""]`` for ``"21.1.2.0005"``."""
        numbers = [int(part) for part in self.version.split(".")[:4]]
        return numbers + [0] * (4 - len(numbers)) + [""]

    def GetVersionString(self):
        return self.version


@dataclass(frozen=True)
class ResolveProfile:
    """One Resolve release/edition as the plugin sees it (docs/RESOLVE_INTEGRATION.md)."""

    name: str
    product: str
    version: str  # what GetVersionString() returns
    has_mark_in_out_api: bool = True  # Timeline.GetMarkInOut (README v19+; early 19.0 builds may lack it)
    python_gated: bool = False  # free edition 21.1+: Python scripts get no usable API

    @property
    def subtitle_api(self) -> bool:
        """21.1 (checked on 21.1.2): an SRT append lands on Subtitle 1 after the timeline's end, whatever
        recordFrame and trackIndex say, and Timeline.MoveItems can move the cues to where they belong."""
        return self.version_tuple[:2] >= (21, 1)

    @property
    def audio_render_api(self) -> bool:
        """21.1 added GetAudioRenderFormats/Codecs and the "AudioFormat" render setting."""
        return self.version_tuple[:2] >= (21, 1)

    @property
    def studio(self) -> bool:
        return "Studio" in self.product

    @property
    def version_tuple(self) -> tuple:
        return tuple(int(part) for part in self.version.split("."))


_FREE, _STUDIO = "DaVinci Resolve", "DaVinci Resolve Studio"

# 21.0.4's timeline-selection calls and 21.1.1's async APIs are not modelled: the plugin uses neither.
PROFILES = {
    profile.name: profile
    for profile in (
        ResolveProfile("19.0-free", _FREE, "19.0.0.0069", has_mark_in_out_api=False),
        ResolveProfile("20.3-free", _FREE, "20.3.0.0023"),
        ResolveProfile("20.3-studio", _STUDIO, "20.3.0.0023"),
        ResolveProfile("21.0.4-free", _FREE, "21.0.4.0009"),
        ResolveProfile("21.0.4-studio", _STUDIO, "21.0.4.0009"),
        ResolveProfile("21.1.2-studio", _STUDIO, "21.1.2.0005"),
        ResolveProfile("21.1.2-free", _FREE, "21.1.2.0005", python_gated=True),
        ResolveProfile("22.0-studio", _STUDIO, "22.0.0.0001"),  # hypothetical, newer than tested
        ResolveProfile("22.0-free", _FREE, "22.0.0.0001", python_gated=True),  # hypothetical
    )
}


def make_media_pool(timeline: FakeTimeline) -> FakeMediaPool:
    """Master/{footage.mov, Timeline 1} + Master/Templates/Lower Thirds/{Caption Style A, Caption Style B}"""
    lower_thirds = FakeFolder(
        "Lower Thirds",
        clips=[
            FakeMediaPoolItem("Caption Style A", kind="Generator"),
            FakeMediaPoolItem("Caption Style B", kind="Generator"),
        ],
    )
    templates = FakeFolder(
        "Templates", subfolders=[lower_thirds], clips=[FakeMediaPoolItem("Plain Text+", kind="Generator")]
    )
    root = FakeFolder(
        "Master",
        clips=[
            FakeMediaPoolItem("footage.mov", file_path="/media/footage.mov"),
            FakeMediaPoolItem("Timeline 1", kind="Timeline"),
        ],
        subfolders=[templates],
    )
    return FakeMediaPool(timeline, root)


def make_world(
    frame_rate="24",
    start_timecode="01:00:00:00",
    drop_frame=False,
    product=None,
    profile: str | ResolveProfile | None = None,
    **timeline_kwargs,
):
    """A Resolve with one project, one timeline (footage on Video 1) and the template bins.
    ``profile`` (a ``PROFILES`` key or a ``ResolveProfile``) sets the product, version and knobs;
    explicit ``product`` / timeline knobs still win."""
    if isinstance(profile, str):
        profile = PROFILES[profile]
    if profile is not None:
        timeline_kwargs.setdefault("has_mark_in_out_api", profile.has_mark_in_out_api)
        timeline_kwargs.setdefault("has_move_items_api", profile.subtitle_api)
    timeline = FakeTimeline(
        frame_rate=frame_rate, start_timecode=start_timecode, drop_frame=drop_frame, **timeline_kwargs
    )
    if profile is not None:
        timeline.subtitle_append_at_timeline_end = profile.subtitle_api
        timeline.subtitle_drops_empty_cues = profile.subtitle_api
    # Some existing content on Video 1 so "never modify existing tracks" is observable.
    footage = FakeMediaPoolItem("footage.mov", file_path="/media/footage.mov")
    timeline.tracks["video"][0].items.append(
        FakeTimelineItem("footage.mov", timeline.start_frame, int(timeline.fps * 60), media_pool_item=footage)
    )
    media_pool = make_media_pool(timeline)
    project = FakeProject(timeline, media_pool)
    if profile is not None:
        project.audio_render_api = profile.audio_render_api
    if profile is None:
        return FakeResolve(project, product=product or "DaVinci Resolve")
    return FakeResolve(
        project, product=product or profile.product, version=profile.version, python_gated=profile.python_gated
    )


def timecode(timeline: FakeTimeline, frame: int) -> str:
    return frames_to_timecode(frame, timeline.fps, timeline.drop_frame)
