# Resolve integration: findings, chosen API calls, risks

How the plugin in `resolve_plugin/` talks to DaVinci Resolve, what was verified from
Blackmagic's scripting README and community sources, and what is still only verifiable
against a live Resolve. Researched October 2026 against Resolve 19/20 (README mirrors up to
21.0.4), with release notes up to 21.1.2 for §9. Blackmagic's own README ships with Resolve as
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
3.13 only on recent builds ([wildlion][wildlion], [forum t=221552][forum-tp2srt]). The
plugin itself is stdlib-only and CI runs its tests on 3.10–3.14 (`plugin` job), so the limit
is what Resolve detects, not the code.
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
content, the cues land where that content ends instead of at `recordFrame`.

**Studio 21.1.2 (headless, 2026-11-01): the append ignores both `recordFrame` and
`trackIndex`.** Every SRT goes onto Subtitle 1, starting at the timeline's end
(`GetEndFrame()`, every track counted) plus the SRT's own times: on the 60 fps test timeline,
`recordFrame` 219600 gave a first cue at 761837 = 761717 + 2 s, and a second SRT appended for
track 2 landed on track 1 after the first one's last cue. `AppendToTimeline` returns only the
first cue. A cue whose start and end are the same millisecond (`00:00:57,030 --> 00:00:57,030`)
is not placed at all. `Timeline.SetCurrentTimecode` before the append is no help: under
`Resolve.exe -nogui` it never returned and hung the scripting server.

What works there is moving the cues afterwards with `Timeline.MoveItems(items, {"trackIndex":
index, "startFrame": first_cue_frame})` (21.x API; `TimelineItem.SetPosition` is its
one-item form). It moves the leftmost item to the position and the others by the same offset,
so the cue spacing is kept, and it puts them on another track. The timeline's end goes back
to where it was. The plugin (`resolve_io.create_subtitle_track`) therefore:

1. adds the track, notes every subtitle item on the timeline (by `GetUniqueId()`), appends;
2. takes the new items (all subtitle items that weren't there before, on whatever track);
3. if they aren't on the new track at the planned first-cue frame, calls `MoveItems` on them;
4. verifies `GetItemListInTrack("subtitle", index)`: the first cue's `GetStart()` against the
   planned frame and the count against the plan's cues that have a length in the SRT.

On any mismatch, or when `MoveItems` is missing or returns False, it deletes the new cues left
on other tracks (`Timeline.DeleteClips`), the new track (`DeleteTrack`) and the imported item
(`DeleteClips`), keeps the per-speaker SRT files, and the summary tells the user where
they are and how to import them (File > Import > Subtitle, then "Insert Selected
Subtitles to Timeline Using Timecode"). The engine's SRTs count from the start of the
rendered range; for manual import at absolute timecode they would need the timeline start
added, which is why the message says to drop them at the in-point.

Checked on Studio 21.1.2 with the range 01:01:00:00–01:01:59:59 and a timeline that already had
clips to 761717: three speakers' tracks (28, 1 and 4 cues), every cue within half a frame of
its SRT time, first cue 219600, user's subtitle tracks and the timeline end untouched.
Studio 19/20: unverified (the forum bug may still apply, and there `MoveItems` may not exist,
which falls back to the `.srt` files as before). Text+ mode does not depend on any of this.

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
- `endFrame` is exclusive on Resolve Studio 21.1.2 (checked headless, 2026-11-01): `endFrame:
  duration - 1` gives a clip one frame short, and a one-frame clip (`endFrame == startFrame`)
  makes the whole `AppendToTimeline` call return None. The plugin places one probe clip with the
  inclusive reading, measures it, and uses `endFrame: duration` when it came out a frame short
  (`resolve_io._probe_end_frame`). A batch Resolve refuses outright is retried clip by clip.
- `AppendToTimeline` clears the timeline's mark in/out on 21.1.2; the plugin reads the marks
  before creating tracks and sets them again afterwards (`Timeline.SetMarkInOut`), so the next
  run renders the same range.
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
  On Studio 21.1.2 the marks are counted from the timeline start (01:01:00:00 on a
  01:00:00:00 timeline at 60 fps reads 3600), while `SetRenderSettings`' `MarkIn`/`MarkOut`
  take absolute frames and overwrite the timeline's marks. `resolve_io.marks_offset` adds
  the start frame when the marks lie before it or fit inside the timeline counted from it,
  and `app.run` puts the marks back after the run in the numbers it read.
- Drop-frame arithmetic (`timing.py`) is the standard 2/4 frames-per-minute skip; tested
  against known values (`00:01:00;02` = 1800 frames, `00:10:00;00` = 17982 at 29.97).

## 5. Rendering the audio

**What doesn't work.** `GetRenderFormats()` returns `{name: extension}` (`"Wave": "wav"`), and
`GetRenderCodecs` / `SetCurrentRenderFormatAndCodec` take the *extension*: the 21.1 stub names
the parameter `renderFormatFileExtension`, and `GetRenderCodecs("QuickTime")` returns `{}` where
`"mov"` returns the codecs ([mcp #59][mcp-59]). Even with `"wav"`, Wave exposes no codecs and
every `("wav", codec)` pair is rejected, measured on Studio 19.1.3.7, 21.0.3.7 and 21.0.4.5
([mcp api-limitations][mcp-limits], [resolve-mcp #32][rmcp-32]). The plugin's first version
passed `"Wave"`, and a real Resolve stopped it with "no audio codec available for the Wave
render format".

**What the plugin does** (`resolve_io.render_audio`), the sequence AutoSubs and
davinci-resolve-mcp use ([AutoSubs core][autosubs-core], [mcp live 21.1 test][mcp-live]):

1. Remember `GetCurrentRenderFormatAndCodec()` and `GetCurrentRenderMode()`.
2. `LoadRenderPreset("Audio Only")`, else any preset from `GetRenderPresetList()` with "audio" in
   its name (the stock name is localised). Failing that, `SetCurrentRenderFormatAndCodec("wav",
   codec)` with the codecs from `GetAudioRenderCodecs("wav")` (21.1+) or `GetRenderCodecs("wav")`,
   then `"lpcm"` / `"LinearPCM"`.
3. `SetCurrentRenderMode(1)`, then `SetRenderSettings` with `TargetDir`, `CustomName`,
   `ExportVideo: False`, `ExportAudio: True`, `MarkIn`/`MarkOut` (the in/out range, or the whole
   timeline from `GetStartFrame()` to `GetEndFrame() - 1`; `SelectAllFrames` only when those can't be
   read, because after a MarkIn/MarkOut job a `SelectAllFrames` job may fail to start), plus
   `AudioFormat: "wav"` (21.1+), `AudioCodec: "lpcm"`, `AudioBitDepth: 16`, `AudioSampleRate:
   48000`. If that returns False, it is retried without `AudioFormat`, then without the audio
   keys. With no preset and no accepted format, only `AudioFormat` can make the render a WAV,
   so then a refusal is reported (check that the Audio Only preset exists).
4. `AddRenderJob()`, `StartRendering([job])` (retried once: on 21.1 it sometimes returns False
   and leaves the job Ready, [mcp #207][mcp-207]), poll `IsRenderingInProgress()` while pumping
   the UI, check `GetRenderJobStatus(job)["JobStatus"]`, `DeleteRenderJob(job)`.
5. Restore the format/codec (with `ExportVideo: True`) and the render mode: settings layer
   onto the current state, so a later video render would otherwise come out audio-only.
6. Find the output by its `CustomName` stem with any extension. `.wav`/`.aif(f)`/`.flac` are
   used; anything else (for example an Audio Only preset left on MXF) is reported by name.

Side effect: the Deliver page's current preset state is changed (paths, audio settings);
saved render presets are not. The WAV goes to a temp dir that is deleted unless the subtitle
fallback needs the SRTs.

**Reusing a render** (`render_cache.py`). Before rendering, `render_cache.fingerprint` hashes
what the audio depends on: the timeline's id and name, the render range, the frame rate and
start, the track selection, and per audio track its enabled state and every item's media,
position, offsets, enabled state and volume/pan properties. A finished render is copied to
`<data>/renders/<fingerprint>.wav`; the next run with the same fingerprint uses it instead
(unless `force_rerender`). On Studio 21.1.2 the fingerprint is the same after the run's marks
are restored and across a Resolve restart (the debug log traces of the reads are identical, and
the run log prints the first 12 characters on a miss).

A run of a two-track timeline stores two entries, and every timeline shares the directory. The
first version kept only the 5 most recently used, so two per-track runs on other timelines
evicted an unchanged timeline's renders, and the 60 fps test timeline rendered again
with nothing changed. Now up to 24 entries and 4 GB (about six hour-long renders) are kept,
least recently used dropped first; the entry just stored is always kept.

## 6. Engine subprocess

`config.json["engine"] + ["run", wav, "--out", dir, flags]`; stdout is read on a thread
and parsed line by line (`engine_runner.py`); `PYTHONPATH`/`PYTHONHOME` from Resolve's
script host are stripped from the child's environment so the engine venv is clean; on
Windows `CREATE_NO_WINDOW` avoids a console flashing up. Cancel terminates the process.

With debug logging on (README, "Debug logs") the plugin adds `--verbose` (engine loggers at DEBUG
and tracebacks on stderr, which the plugin already reads) and records the command, the
environment changes, every raw stdout/stderr line and the exit code in the run's log. The same
log traces every Resolve API call (`debug_log.Tracer`, a transparent proxy around `resolve` and
every object it returns) and, just before rendering, what `GetRenderFormats()` /
`GetRenderCodecs()` (by format name and by extension), `GetRenderPresetList()`,
`GetCurrentRenderFormatAndCodec()`, `GetCurrentRenderMode()` and `GetRenderSettings()` return.

## 7. Fixing speakers before tracks are written

Diarization mistakes (one person split into two speakers, a line given to the wrong
person, two people heard as one) are fixed in the review dialog's **Fix speakers…** window,
before anything touches the timeline. The model is `speaker_edits.py` (pure, stdlib): edits
are operations on the parsed `transcript.json` dict, kept on a stack and replayed from the
original, so every one is undoable:

- *rename*: the speaker's name (`name_source: "user"`)
- *merge B into A*: B's cues, segments and words become A's; A keeps its name
- *reassign*: selected cues move to another existing speaker (their segments follow)
- *split*: selected cues move to a new speaker (`SPEAKER_NN`, the first free id)

The window (`ui/speaker_editor.py` holds the logic both backends share) has a speaker list
with rename and "merge into…", a cue list (number, timeline timecode, speaker, text) that
can be filtered by speaker, "move selected to" another or a new speaker, and Undo. Names
typed in the review rows go into the editor as renames, and the review rows are rebuilt
from it when the window closes. Planning then uses the edited transcript; when cues changed
speaker, the per-speaker SRTs are rewritten (`out/edited/<id>.srt`) because the engine's no
longer match.

When the user clicks Create tracks, the operations are saved as `edits.json` next to the
run's `transcript.json` and as `<data>/edits/<sha256 of the rendered WAV>.json` (the temp
run dir is normally deleted). A later run whose rendered audio has the same hash re-applies
them: cues are found by index when their times still match, otherwise by time (±50 ms);
operations that no longer fit are skipped and the dialog says how many. Undoing every fix
and creating tracks removes the saved file.

UIManager has no list-with-columns widget besides `ui.Tree`, so both lists are Trees
(`NewItem` / `Text[n]` / `AddTopLevelItem` / `SetHeaderItem`, `CurrentItem()` /
`SelectedItems()`); the review window is closed and rebuilt around the editor rather than
edited in place.

## 8. Highlighting words as they're spoken (Text+)

An optional review-dialog toggle, **Highlight words as they're spoken**, off by default, with a
highlight colour (default `#FFD400`, a bright accent; the other words keep the template's own
colour). Both are remembered in `config.json` (`"highlight_words"`, `"highlight_color"`,
written by `config.save_settings`, which re-reads the file and changes only those keys). It
applies to Text+ captions only; subtitle tracks can't be styled per word.

### Options considered

1. **One clip per cue, keyframed Character Level Styling (CLS).** One clip, but the styling
   would have to be animated. Nobody has shown this working from a script: KathaCut's live
   spike on Resolve Studio 21 never got a keyframed CLS write to render ([KathaCut ADR 0011][kc-adr]
   "Keyframing (W3)": no data). Forum users report hand-keyframed CLS keeping only the last
   value ([forum p=741898][forum-cls-keys]) and crashing Resolve ([forum t=131845][forum-cls-crash]).
   Fusion's `SetInput(name, value, time)` only keys an input that already has a spline
   ([Fusion 8 Scripting Guide][fusion-scripting]), and text modifiers on `StyledText` only attach
   by registry id ([davinci-resolve-mcp #250][mcp-250]). AutoSubs, the best-known open-source
   Resolve captioner, lights words only inside its own bundled macro, whose Lua callback writes
   the keys. User templates get plain text ([auto-subs caption_style.lua][autosubs-style]).
2. **One clip per word step, static CLS (chosen).** Each cue becomes contiguous clips, one per
   word (and one per long pause), each a copy of the user's template showing the **whole cue**,
   with only the spoken word recoloured. This needs no keyframes, works with any Text+ template,
   and every clip is an ordinary Text+ title. KathaCut reached the same conclusion
   ("split clips") after its spikes ([ADR 0011][kc-adr], "Consequences").

### How the colour is written

`SetInput("CharacterLevelStyling", …)`, `LoadSettings` (table or file) and `comp:Paste` all
return without error but render plain. KathaCut tested each on Resolve Studio 21.0.0.47
([ADR 0011][kc-adr], "Write spike result"). What works there is a comp file round trip
(method D), which `resolve_io.apply_word_highlight` does for each lit clip:

```python
item.ExportFusionComp(path, 1)                 # README: (path, compIndex) --> Bool
text = word_highlight.add_highlight_to_comp(open(path).read(), cue_text, entries)
item.ImportFusionComp(path)                    # README: --> fusionComp ("creating and adding a new" comp)
item.LoadFusionCompByName(newest)              # only if the import added a comp instead of replacing it
```

The edit (`word_highlight.add_highlight_to_comp`, pure and tested) adds a `StyledTextCLS`
tool shaped like the one Resolve saves for hand-applied styling, and points the Text+ tool's
`StyledText` at it:

```lua
BetterCaptionsHighlight = StyledTextCLS { Inputs = {
  CharacterLevelStyling = Input { Value = StyledText { Array = {
    { 2401, 4, 10, Value = 1 }, { 2402, 4, 10, Value = 0.831373 }, { 2403, 4, 10, Value = 0 },
  }, Value = "" } },
  Text = Input { Value = "Hey Jordan,\nhow are you?" }, ... } },
Template = TextPlus { Inputs = { StyledText = Input { SourceOp = "BetterCaptionsHighlight", Source = "StyledText" }, ... } }
```

`2401`/`2402`/`2403` are fill red/green/blue (0..1). Ranges are 0-based code points, end
inclusive ([ADR 0011][kc-adr], "Property ids", "Counting unit"); `"\n"` between lines is
counted as one. Everything else in the exported comp is left byte for byte, so font, size,
outline, shadow, position and any animation are the template's. A template whose
`StyledText` is already driven by another tool (its own CLS, a Follower, an expression) is not
overridden: the track is rebuilt with one plain clip per cue and the summary says why. The
same fallback applies when the first round trip fails (for example `ExportFusionComp` missing
or refused). A failure later in the track leaves that clip showing the cue unlit, and the
summary lists it.

Editing afterwards: each clip's text is now in the *Character Level Styling* modifier (Inspector
> Modifiers), and the colour can be changed there. Retiming is ordinary clip trimming. Changing
the look of all clips at once means changing the template and re-running.

### Timing (`word_highlight.py`, pure)

- `cue_words`: the engine builds a cue's text by joining its words' stripped text, so cue
  tokens are matched in order to the transcript words (`segments[].words`) around the cue,
  trying the cue speaker's words first and then anyone's (speaker fixes relabel segments only
  roughly). Punctuation and case are ignored when matching. Tokens without a word share the
  time between their neighbours. A cue with no matching word stays one plain clip; its times
  are never guessed.
- `highlight_steps`: the placed cue's frames (after `place_cues` trimmed overlaps) are cut into
  steps that are contiguous and cover the cue exactly, so the caption never blinks. A word is
  lit from its first frame. A pause of up to 0.5 s after it, or before the first word, stays on
  that word; a longer one is an unlit step. Words are clamped to the cue's frames, so words
  spanning a cue boundary are cut and words wholly outside are dropped. A word shorter than a
  frame still gets one frame when there is room.

Cost: one clip per word, and the round trip takes about 0.5 s per clip, mostly the clip insert
([ADR 0011][kc-adr], "Timing"). A long timeline takes minutes to write, during which Resolve is
busy.

## 9. Resolve 21.1 and later

**Resolve 21.1 (September 2026) moved Python scripting to Studio.** Blackmagic's statement,
quoted in AYON's Resolve addon 0.6.4, is "We have moved the ability to script in Python to the
Studio version"; the addon now refuses to start on non-Studio 21.1+ ([ayon-resolve 0.6.4][ayon-064]).
Python 2 support is gone and 21.1 added about 20 scripting calls ([CineD][cined-211]); whether
the free edition keeps Lua is not stated. 21.1.1 added async calls (Quick Export, transcription,
scene-cut detection, Smart Reframe, subtitle creation) ([Digital Production][dp-2111]); 21.1.2
followed on October 26 with unspecified "scripting API improvements" ([Newsshooter][ns-2112]). None of these notes document a removal or change of the calls this
plugin uses (§2–§5, §8).

So: **free edition up to 21.0.x, Studio for 21.1 and later.** What the free 21.1 edition does
with a Python script from Workspace > Scripts is not documented: the script may not be listed
or run at all, may get `resolve` = None, or may get an object whose calls return None.

**What the plugin does** (`compat.py`, checked in `app.main` before any UI or project call):
`compat.detect(resolve)` reads `GetVersion()` (falling back to `GetVersionString()`) and
`GetProductName()`, each failure leaving that part unknown.

- `resolve` is None: an error saying Python scripting needs Studio since 21.1 (and, for
  Studio, to start it from Workspace > Scripts). Nothing is rendered or created.
- Free edition, 21.1 or newer: an error naming the version and saying it needs Studio 21.1+
  or free 21.0.x. Same: nothing touched, and UIManager is never tried.
- Free edition, version unknown (the calls return None), and `GetProjectManager()` or
  `GetCurrentProject()` returns nothing: the same Studio message instead of "open a project
  first" (`resolve_io.current_project_and_timeline`).
- Otherwise the run goes ahead; the progress log starts with `Resolve: <product> <version>`,
  and a version newer than `compat.TESTED_UP_TO` (21.1) adds a "newer than tested" warning to
  the summary.

When there is no injected backend the errors above are printed to Resolve's Console; a
tkinter dialog is shown too when `fusion` or `bmd` is present (i.e. inside Resolve's script
host). The UI choice (§1) uses the same edition check.

Unverified: what free 21.1.x actually gives a Python script (which of the three cases, and
whether `fusion`/`bmd` still arrive), whether `GetProductName()` still says "Studio" on
Studio 21.1.x, and whether the plugin's calls behave the same on Studio 21.1.2.

## Still to verify on a real Resolve (free and Studio)

1. Subtitle placement (§2) on 19/20: whether `recordFrame` is honoured there and whether
   `MoveItems` exists (Studio 21.1.2 is checked).
2. Placed Text+ clip durations (§3 quirk) and that `FindToolByID("TextPlus")` finds the
   node in user templates (nested macros may need `GetToolList`).
3. `GetMarkInOut()` shape on 19.0 (older builds may lack it; handled).
4. The audio render (§5) on 21.1.2: that the Audio Only preset loads and `AudioFormat: "wav"` is
   accepted, the output is one `.wav`, and the user's render format is restored afterwards.
5. tkinter dialogs appearing in front of Resolve's window (topmost is requested).
6. The UIManager "Fix speakers" window (§7): `ui.Tree` selection (`SelectionMode`
   `"ExtendedSelection"`, `SelectedItems()` returning a 1-based table), `ItemClicked`'s
   `item`, and `ComboBox.Clear()` firing `CurrentIndexChanged` (guarded).
7. Word highlighting (§8): that the comp round trip renders the lit word through the Python
   bridge on 19/20 (KathaCut verified it from Lua on Studio 21), whether `ImportFusionComp`
   adds or replaces the comp on a Text+ title, how `"\n"` counts in CLS ranges on two-line
   cues, and wall-clock time per clip on a long timeline.
8. Resolve 21.1.x (§9): what the free edition gives a Python script (not listed, `resolve` =
   None, or calls returning None) and that the plugin's message shows; that Studio 21.1.2
   reports "Studio" in `GetProductName()` and a parseable `GetVersion()`, and runs the whole
   flow unchanged.

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
[kc-adr]: https://github.com/vxlabs/KathaCut/blob/main/docs/decisions/0011-resolve-character-level-styling.md
[autosubs-style]: https://github.com/tmoroney/auto-subs/blob/main/AutoSubs-App/src-tauri/resources/modules/caption_style.lua
[forum-cls-keys]: https://forum.blackmagicdesign.com/viewtopic.php?p=741898
[forum-cls-crash]: https://forum.blackmagicdesign.com/viewtopic.php?f=21&t=131845
[fusion-scripting]: https://documents.blackmagicdesign.com/UserManuals/Fusion8_Scripting_Guide.pdf
[mcp-250]: https://github.com/samuelgursky/davinci-resolve-mcp/issues/250
[ayon-064]: https://github.com/ynput/ayon-resolve/releases/tag/0.6.4
[cined-211]: https://www.cined.com/davinci-resolve-21-1-released-ai-assistant-integration-via-mcp-individual-hdr-trims-and-python-scripting-moves-to-studio/
[dp-2111]: https://digitalproduction.com/2026/10/06/davinci-resolve-21-1-1/
[ns-2112]: https://www.newsshooter.com/2026/10/26/davinci-resolve-21-1-2/
[mcp-59]: https://github.com/samuelgursky/davinci-resolve-mcp/issues/59
[mcp-limits]: https://github.com/samuelgursky/davinci-resolve-mcp/blob/main/docs/reference/api-limitations.md
[mcp-live]: https://github.com/samuelgursky/davinci-resolve-mcp/blob/main/tests/live_resolve211_normalization.py
[mcp-207]: https://github.com/samuelgursky/davinci-resolve-mcp/issues/207
[rmcp-32]: https://github.com/danielbaldwin47/resolve-mcp/issues/32
[autosubs-core]: https://github.com/tmoroney/auto-subs/blob/main/AutoSubs-App/src-tauri/resources/modules/autosubs_core.lua
