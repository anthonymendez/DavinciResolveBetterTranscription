# TODO

- [ ] **Test in a real DaVinci Resolve.** Run Workspace > Scripts > Better Captions on a short
      two-speaker clip in Resolve free and Studio (19/20). Verify both output modes:
  - Subtitle tracks per speaker: Resolve has a known bug where imported SRT cues can land after the
    last video clip instead of at their record frame. The plugin verifies placement and falls back
    to leaving the per-speaker `.srt` files for manual import; confirm which behaviour you get.
  - Text+ tracks per speaker from a bin template: check timing on 23.976, 29.97 DF and a
    01:00:00:00 start timecode, and that the template styling is kept.
  - First run downloads the speech models (several GB for `large-v3-turbo`); check the progress UI.
  - See `docs/RESOLVE_INTEGRATION.md` for the API calls used and their known risks.
- [ ] **Turn on brindle's pre-merge checks.** Add to `.brindle/config.json`:
      `"checks": ["uv run pytest -q"]`
      so every worker branch runs the full suite before it merges.
