# Better Captions for DaVinci Resolve

Speaker-aware transcription for DaVinci Resolve: detects who is speaking, names
speakers from context, and builds per-speaker subtitle tracks or styled Text+
caption tracks using templates from your media pool.

## Install

Windows (PowerShell):

    irm https://raw.githubusercontent.com/anthonymendez/DavinciResolveBetterTranscription/main/install/install.ps1 | iex

macOS / Linux:

    curl -fsSL https://raw.githubusercontent.com/anthonymendez/DavinciResolveBetterTranscription/main/install/install.sh | bash

Then in Resolve: **Workspace > Scripts > Better Captions**.

No admin rights or sudo needed: the installer sets up [uv](https://docs.astral.sh/uv/)
if missing, puts the engine in its own Python 3.12 venv in your user data dir,
and copies the plugin into Resolve's `Scripts/Utility` folder. An NVIDIA GPU
with CUDA is used automatically if available. Re-run the same command to upgrade.

Options (environment variables):

- `BETTER_CAPTIONS_REF`: git branch/tag to install (default `main`)
- `BETTER_CAPTIONS_HOME`: install somewhere other than the default data dir
- `BETTER_CAPTIONS_SOURCE`: install from a local checkout instead of GitHub

To uninstall, run `install/uninstall.ps1` or `install/uninstall.sh` the same
way (e.g. `curl -fsSL .../install/uninstall.sh | bash`).

See `docs/ARCHITECTURE.md` for how it fits together.
