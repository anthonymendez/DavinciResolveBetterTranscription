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

The installer sets up [uv](https://docs.astral.sh/uv/) if missing, puts the
engine in its own Python 3.12 venv in your user data dir, and copies the plugin
into Resolve's `Scripts/Utility` folder. An NVIDIA GPU with CUDA is used
automatically if available. Re-run the same command to upgrade.

Requirements: Resolve runs the plugin with a system Python it finds by itself,
a 64-bit Python 3.10–3.12 with tkinter. If you already have one where Resolve
looks, the installer uses it; otherwise:

- Windows: installs Python 3.12 from python.org for all users (via winget when
  available). This is the only step that needs admin rights: Windows shows one
  UAC prompt, because Resolve only finds Python installed under `C:\Program Files`.
- macOS: offers to install the python.org Python 3.12 package, which runs
  `sudo installer` and asks for your password. Nothing is installed without your
  yes; when run non-interactively it only tells you what to install.
- Linux: installs no system packages; it prints the command to run, e.g.
  `sudo apt-get install python3 python3-tk`.

Restart Resolve after Python is installed.

Options (environment variables):

- `BETTER_CAPTIONS_REF`: git branch/tag to install (default `main`)
- `BETTER_CAPTIONS_HOME`: install somewhere other than the default data dir
- `BETTER_CAPTIONS_SOURCE`: install from a local checkout instead of GitHub
- `BETTER_CAPTIONS_SKIP_SYSTEM_PYTHON=1`: skip the system Python check/install

To uninstall, run `install/uninstall.ps1` or `install/uninstall.sh` the same
way (e.g. `curl -fsSL .../install/uninstall.sh | bash`).

See `docs/ARCHITECTURE.md` for how it fits together.
