# Resolve integration: findings, chosen API calls, risks

How the plugin in `resolve_plugin/` talks to DaVinci Resolve, what was verified from
Blackmagic's scripting README and community sources, and what is still only verifiable
against a live Resolve. Researched October 2026 against Resolve 19/20 (README mirrors up to
21.0.4). Blackmagic's own README ships with Resolve as
`Developer/Scripting/README.txt` (Help > Documentation > Developer); its text is mirrored
verbatim in [X-Raym's gist][xraym-gist] and summarised at [resolvedevdoc][devdoc].

## 1. Does a Python script in Workspace > Scripts run on Resolve free 19/20?

**Yes, but only with a system Python installed; Resolve does not bundle one.**

- Blackmagic's README, *Prerequisites*: "DaVinci Resolve scripting requires one of the
  following to be installed (for all users): Lua 5.1, Python >= 3.6 64-bit, Python 2.7
  64-bit" ([X-Raym gist][xraym-gist], [devdoc readme][devdoc]). Resolve 20 dropped
  Python 2.
- Scripts in `Fusion/Scripts/Utility` are listed under Workspace > Scripts on every page
  and get the globals `resolve`, `fusion` and `bmd` ([README][xraym-gist],
  [den.dev][dendev]). A `.py` file only shows up (and runs) once Resolve finds a Python
  interpreter: a forum report of a Utility script missing on macOS until Python was
  installed ([forum t=183687][forum-missing]).
- The free edition runs scripts from the Scripts menu and the Console; what it does not
  do is *external* scripting (a separate process using `DaVinciResolveScript`), which is
  Studio-only ([forum t=113252][forum-free], [wildlion guide][wildlion],
  [Pulse Edit][pulseedit]). Hence a Lua launcher that spawned the engine's venv Python
  would not help: the Resolve-side code must run in-process, as a Python (or Lua) script.
  Three existing tools confirm Python-in-Resolve on the free edition:
  [Resolve-OpenCaptions][opencaptions] ("Compatible with both DaVinci Resolve Free and
  Studio"), [davinci-srt-to-timeline][srt2tl] ("Works in free Resolve"),
  [den.dev's captions][dendev].
- **UIManager is Studio-only since 19.1.** Fusion's UIManager (the dialog toolkit scripts
  normally use) was gated in Resolve 19.1; on the free edition a script that creates a
  UIManager window gets a "requires Studio" prompt. Blackmagic staff called it intentional
  ("scripting through any UI has been the domain of the Studio version")
  ([forum t=213158][forum-uim], [forum t=216545][forum-removed],
  [X-Raym docs note][xraym-docs]). OpenCaptions and srt-to-timeline therefore use tkinter
  / native OS dialogs instead.

**What the plugin does:** `resolve_plugin/Utility/Better Captions.py` is the only script
Resolve lists. It loads the stdlib-only package `better_captions_plugin` from
`better_captions_plugin.zip` next to it (zipimport; a package *folder* would make Resolve
list every module as a menu entry). UI backend selection (`ui/__init__.py`):
`fusion.UIManager` when `resolve.GetProductName()` contains "Studio", tkinter otherwise;
`"ui": "tk" | "uimanager" | "auto"` in `config.json` overrides. The launcher refuses
Python < 3.8 with a clear message.

### Installer requirements (for the system-Python task)

Resolve finds Python by itself; nothing in the plugin needs environment variables. It
needs a **64-bit CPython 3.10–3.12 installed for all users with tkinter**, from python.org
(not Homebrew/pyenv/Microsoft Store builds, which Resolve often does not detect;
[OpenCaptions readme][opencaptions], [forum t=221552][forum-tp2srt], [wildlion][wildlion]).
Per OS, what Resolve looks for:

| OS | Python Resolve detects | tkinter |
|---|---|---|
| Windows | python.org installer with "Install for all users" (`C:\Program Files\Python3xx\`; registry key `HKLM\SOFTWARE\Python\PythonCore\3.xx`). Resolve also honours `RESOLVE_PYTHON3_HOME` pointing at that folder ([catdv/ayon notes][ayon]). | included by default ("tcl/tk and IDLE" option) |
| macOS | python.org `.pkg` (`/Library/Frameworks/Python.framework/Versions/3.xx`), which also links `/usr/local/bin/python3`. | included in the python.org pkg |
| Linux | `python3` on PATH (`/usr/bin/python3`), 64-bit. | distro package `python3-tk` |

Versions: README says ≥ 3.6; community experience is that 3.10–3.12 work on 19/20 and
3.13 only on recent builds ([wildlion][wildlion], [forum t=221552][forum-tp2srt]).
After installing Python, Resolve must be restarted to pick the script up. The installers
already copy `resolve_plugin/Utility/*` (the launcher and the zip).

## 2. Importing an SRT onto a specific subtitle track

**There is no dedicated API.** The README's only subtitle-related calls are the
`"subtitle"` track type in `AddTrack`, `DeleteTrack`, `GetTrackCount`, `GetTrackName`,
`SetTrackName`, `GetItemListInTrack`, `SetTrackEnable`, `SetTrackLock`, plus
`Timeline.CreateSubtitlesFromAudio()` (Studio) and the `ExportSubtitle` render flag
([X-Raym gist][xraym-gist]). `ImportIntoTimeline` is AAF-only. Older forum answers said
subtitle clips cannot be created by script at all ([forum t=131806][forum-nosub],
[forum t=114570][forum-getname]).

The one documented-call route, used by a Lua script on the forum
([forum t=208658][forum-lua-srt]):

```python
items = media_pool.ImportMedia([srt_path])          # an SRT becomes a subtitle MediaPoolItem
timeline.AddTrack("subtitle")                        # new track; never reuse an existing one
index = timeline.GetTrackCount("subtitle")
timeline.SetTrackName("subtitle", index, speaker_name)
media_pool.AppendToTimeline([{"mediaPoolItem": items[0], "trackIndex": index, "recordFrame": origin_frame}])
```

`AppendToTimeline`'s clipInfo keys `trackIndex` and `recordFrame` are documented
([README][xraym-gist]). The forum poster reports a bug: when the video track already has
content, the cues land where that content ends instead of at `recordFrame`. The plugin
(`resolve_io.create_subtitle_track`) therefore verifies the result with
`GetItemListInTrack("subtitle", index)` and `GetStart()` against the planned first-cue
frame; on any mismatch it deletes the new track (`DeleteTrack`) and the imported item
(`DeleteClips`), keeps the per-speaker SRT files, and the summary tells the user where
they are and how to import them (File > Import > Subtitle, then "Insert Selected
Subtitles to Timeline Using Timecode"). The engine's SRTs count from the start of the
rendered range; for manual import at absolute timecode they would need the timeline start
added, which is why the message says to drop them at the in-point.

Status: **unverified against a live Resolve** (it is the documented path, but the forum
bug may still exist in 19/20). Text+ mode is the robust path.

## 3. Appending a Text+ template and setting its text

Verified by three independent working scripts ([Resolve-OpenCaptions][opencaptions],
[srt-to-timeline][srt2tl], [forum "Script Hack"][forum-hack], [den.dev][dendev]):

```python
# template: a Text+/Fusion title the user put in any media pool bin
timeline.AddTrack("video"); index = timeline.GetTrackCount("video")
timeline.SetTrackName("video", index, speaker_name)
items = media_pool.AppendToTimeline([{
    "mediaPoolItem": template, "startFrame": 0, "endFrame": duration - 1,
    "trackIndex": index, "recordFrame": absolute_frame, "mediaType": 1,
} for each cue])
comp = item.GetFusionCompByIndex(1)
comp.FindToolByID("TextPlus").SetInput("StyledText", text)   # fallback: comp.GetToolList(False, "TextPlus")
```

- Template discovery: walk `MediaPool.GetRootFolder()` with `GetSubFolderList()` /
  `GetClipList()` recursively; a Text+ template is an item whose
  `GetClipProperty("File Path")` is empty and whose `Type` is not a timeline /
  compound / multicam (OpenCaptions uses the empty file path; `Type` values are localised
  strings, so they are only used to exclude). The dialog shows `name (bin path)` so the
  user picks the right one per speaker.
- Known quirk: the placed clip's duration can differ from `endFrame - startFrame + 1`
  (OpenCaptions measures a "duration multiplier" with a probe clip). The plugin checks
  `GetDuration()` per clip and lists mismatches in the summary instead of guessing.
- Timeline items of a Fusion title have one comp (`GetFusionCompCount() == 1`).
- Undo: Resolve's API edits are not grouped into one undo step; because only new tracks
  are created (named after the speaker), the user can delete those tracks to revert.

## 4. Timing

- `timeline.GetSetting("timelineFrameRate")` returns e.g. `"24"`, `"29.97"` or
  `"29.97 DF"` (the README documents the `" DF"` suffix for drop frame); fallback to the
  project setting. `timelineDropFrameTimecode` is read when present, and a `;` in
  `GetStartTimecode()` also means drop frame.
- Positions are absolute frames: `Timeline.GetStartFrame()` (fallback: parse
  `GetStartTimecode()`, drop-frame aware) + `round(seconds * fps)`. `recordFrame` and
  `TimelineItem.GetStart()` use the same space ([README][xraym-gist]; the v20.3 reference
  gist notes "recordFrame is an absolute frame position"). den.dev reports an older
  Resolve returning `29` for `timelineFrameRate`; the plugin parses whatever string comes
  back and the tests cover 23.976 / 29.97 DF / 59.94 DF.
- In/out range: `Timeline.GetMarkInOut()` (returns `{"video": {"in", "out"},
  "audio": {...}}`, README v19+); when the call is missing or no marks are set, the whole
  timeline is used. Cue frames count from the range start (`origin_frame`).
- Drop-frame arithmetic (`timing.py`) is the standard 2/4 frames-per-minute skip; tested
  against known values (`00:01:00;02` = 1800 frames, `00:10:00;00` = 17982 at 29.97).

## 5. Rendering the audio

`project.GetRenderFormats()` → the format whose extension is `wav`; `GetRenderCodecs(fmt)`
→ the codec whose description/name contains "pcm"; `SetCurrentRenderFormatAndCodec`,
`SetCurrentRenderMode(1)` (single clip), `SetRenderSettings({"TargetDir", "CustomName",
"ExportVideo": False, "ExportAudio": True, "SelectAllFrames" or "MarkIn"/"MarkOut"})`,
`AddRenderJob()`, `StartRendering([job])`, poll `IsRenderingInProgress()` while pumping the
UI, check `GetRenderJobStatus(job)["JobStatus"]`, `DeleteRenderJob(job)`, restore the render
mode. All documented in the README ([X-Raym gist][xraym-gist]). Side effect: the Deliver
page's current render settings are changed (format/codec/paths); render presets are not.
The WAV goes to a temp dir that is deleted unless the subtitle fallback needs the SRTs.

## 6. Engine subprocess

`config.json["engine"] + ["run", wav, "--out", dir, flags]`; stdout is read on a thread
and parsed line by line (`engine_runner.py`); `PYTHONPATH`/`PYTHONHOME` from Resolve's
script host are stripped from the child's environment so the engine venv is clean; on
Windows `CREATE_NO_WINDOW` avoids a console flashing up. Cancel terminates the process.

## Still to verify on a real Resolve (free and Studio)

1. Subtitle placement (§2) and whether `ImportMedia` returns the SRT item.
2. Placed Text+ clip durations (§3 quirk) and that `FindToolByID("TextPlus")` finds the
   node in user templates (nested macros may need `GetToolList`).
3. `GetMarkInOut()` shape on 19.0 (older builds may lack it; handled).
4. The exact format/codec names for WAV on each OS (`Wave` / `Linear PCM` expected).
5. tkinter dialogs appearing in front of Resolve's window (topmost is requested).

[xraym-gist]: https://gist.github.com/X-Raym/2f2bf453fc481b9cca624d7ca0e19de8
[xraym-docs]: https://extremraym.com/cloud/resolve-scripting-doc/
[devdoc]: https://resolvedevdoc.readthedocs.io/en/latest/readme_resolveapi.html
[forum-free]: https://forum.blackmagicdesign.com/viewtopic.php?f=21&t=113252
[forum-missing]: https://forum.blackmagicdesign.com/viewtopic.php?f=21&t=183687
[forum-uim]: https://forum.blackmagicdesign.com/viewtopic.php?f=12&t=213158
[forum-removed]: https://forum.blackmagicdesign.com/viewtopic.php?f=21&t=216545
[forum-lua-srt]: https://forum.blackmagicdesign.com/viewtopic.php?f=12&t=208658
[forum-nosub]: https://forum.blackmagicdesign.com/viewtopic.php?f=21&t=131806
[forum-getname]: https://forum.blackmagicdesign.com/viewtopic.php?f=21&t=114570
[forum-hack]: https://forum.blackmagicdesign.com/viewtopic.php?f=21&t=190118
[forum-tp2srt]: https://forum.blackmagicdesign.com/viewtopic.php?f=21&t=221552
[opencaptions]: https://github.com/david-ca6/Resolve-OpenCaptions
[srt2tl]: https://github.com/moshjsmohamedisa-png/davinci-srt-to-timeline
[dendev]: https://den.dev/blog/davinci-resolve-automatic-captions-free/
[wildlion]: https://wildlion.media/davinci-resolve-python-scripting-the-complete-guide-to-the-api/
[pulseedit]: https://pulseedit.com/davinci-resolve-19-scripting-api-fix.html
[ayon]: https://docs.ayon.dev/docs/addon_resolve_admin
