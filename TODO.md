# TODO

- [ ] **Test in a real DaVinci Resolve.** Run Workspace > Scripts > Better Captions on a short
      two-speaker clip in Resolve Studio (19/20). Verify both output modes:
  - Subtitle tracks per speaker: Resolve has a known bug where imported SRT cues can land after the
    last video clip instead of at their record frame. The plugin verifies placement and falls back
    to leaving the per-speaker `.srt` files for manual import; confirm which behaviour you get.
  - Text+ tracks per speaker from a bin template: check timing on 23.976, 29.97 DF and a
    01:00:00:00 start timecode, and that the template styling is kept.
  - Text+ with "Highlight words as they're spoken": the spoken word shows in the highlight colour
    on one- and two-line captions, the rest keeps the template look, and how long a 10-minute
    timeline takes to write (`docs/RESOLVE_INTEGRATION.md` §8).
  - First run downloads the speech models (several GB for `large-v3-turbo`); check the progress UI.
  - See `docs/RESOLVE_INTEGRATION.md` for the API calls used and their known risks.
- [x] **Studio 21.1.2, headless (2026-11-01).** The whole flow ran through the scripting API on a
      60 fps, 01:00:00:00 timeline with a GUI-style in/out range: the log's first line reads
      `Resolve: DaVinci Resolve Studio 21.1.2`, the range renders, Text+ (plain and word
      highlighting) lands on the right frames at full length, and the marks survive the run.
      Subtitle tracks land after the last clip there, so the `.srt` fallback is what you get.
      Still to see in the GUI: the progress window, the review / Fix speakers dialogs and the
      highlight colour; and 23.976 / 29.97 DF timing.
- [x] **The last cue no longer runs past the range's out point** (one ended 43 frames after it
      on Studio 21.1.2): the engine stops short cues' minimum-length stretch at the end of the
      audio, and the plugin trims clips to the rendered range.

Only Resolve Studio is a target; the free edition (no Python scripting since 21.1) is not.
