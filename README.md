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

See `docs/ARCHITECTURE.md` for how it fits together.
