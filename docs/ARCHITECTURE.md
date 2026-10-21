# Architecture

Two halves, joined by a subprocess + JSON contract:

1. **Engine** (`src/better_captions/`): a normal Python 3.12 package installed by
   the installer into its own venv (managed by `uv`). Heavy deps live here only.
   `better-captions run AUDIO --out DIR [...]` writes:
   - `DIR/transcript.json`: a `schema.Transcript` (speakers with inferred names, segments, cues)
   - `DIR/<SPEAKER_ID>.srt`: one SRT per speaker
   - stdout: one JSON event per line: `{"event":"progress","stage":..,"pct":..}`,
     then `{"event":"done",...}` or `{"event":"error","message":..}`.
2. **Resolve plugin** (`resolve_plugin/`): runs inside Resolve (Workspace > Scripts).
   Must not import the engine or any third-party package. It renders timeline
   audio to WAV, runs the engine, shows progress, lets the user review/edit
   speaker names and pick a Text+ template per speaker, fix diarization mistakes
   ("Fix speakers…": merge, move cues to another or a new speaker, undo; `speaker_edits.py`,
   saved as `DIR/edits.json` and re-applied on a re-run of the same audio), then writes either
   per-speaker subtitle tracks or per-speaker Text+ video tracks.
   Source lives in `resolve_plugin/src/better_captions_plugin/`; the installed form is
   `resolve_plugin/Utility/Better Captions.py` (launcher) + `better_captions_plugin.zip`
   (built by `uv run python resolve_plugin/build_plugin_zip.py`; a test fails when stale).
   Resolve runs it with the system Python it finds (3.10–3.12 from python.org); dialogs use
   UIManager on Studio and tkinter on the free edition. See `docs/RESOLVE_INTEGRATION.md`.

## Engine modules (stub signatures are the contract; do not change them without updating callers)

| Module | Job |
|---|---|
| `schema.py` | dataclasses + JSON (de)serialisation, stdlib only |
| `engine/transcribe.py` | faster-whisper, word timestamps; `--backend` picks an alternative from `engine/backends/` |
| `engine/gpu.py` | NVIDIA GPU detection; makes the pip-installed CUDA runtime loadable (`docs/GPU.md`) |
| `engine/backends/whispercpp.py` | whisper.cpp + Vulkan (AMD/Intel/NVIDIA GPUs, Windows/Linux); binary in `<data>/bin`, see `docs/GPU_VULKAN.md` |
| `engine/diarize.py` | speaker turns; default backend sherpa-onnx (no HF token needed), optional pyannote |
| `engine/align.py` | words -> speakers -> segments |
| `naming/` | speaker names from context: heuristic + Anthropic / OpenAI-compatible (Ollama) LLM |
| `secrets.py` | LLM API keys in the OS keychain (`keyring`); `better-captions key set/delete/status/migrate` |
| `cues.py` | segments -> readable caption cues |
| `export/srt.py` | SRT per speaker |
| `pipeline.py`, `cli.py` | orchestration, JSON-lines events |

## Install layout (contract between installer and plugin)

- Data dir: Windows `%LOCALAPPDATA%\BetterCaptions`, macOS `~/Library/Application Support/BetterCaptions`,
  Linux `${XDG_DATA_HOME:-~/.local/share}/BetterCaptions`. Overridable with env `BETTER_CAPTIONS_HOME`.
- `<data>/models/`: downloaded model files; `<data>/bin/`: downloaded native tools (whisper.cpp).
- `<data>/venv/`: engine venv. `<data>/config.json`:
  `{"engine": ["<abs path to venv python>", "-m", "better_captions.cli"], "version": "x.y.z", "gpu": "cuda"|"none"}`
  plus optional user settings (e.g. `"naming_provider"`). LLM API keys live in the OS keychain
  (`secrets.py`, service `better-captions`) or env; plain-text `anthropic_api_key` / `openai_api_key`
  here are legacy, still read with a warning, and moved out by `better-captions key migrate`.
  `"gpu"` records whether the installer added the NVIDIA CUDA runtime (the `cuda` extra); see `docs/GPU.md`.
- Everything under `resolve_plugin/Utility/` is copied into Resolve's `Fusion/Scripts/Utility/` folder:
  - Windows `%APPDATA%\Blackmagic Design\DaVinci Resolve\Support\Fusion\Scripts\Utility`
  - macOS `~/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility`
  - Linux `~/.local/share/DaVinciResolve/Fusion/Scripts/Utility`

## Testing

`uv run pytest -q`. Tests never download models or call Resolve: model stages are faked,
Resolve is a fake object model in `tests/resolve/`.
