#!/usr/bin/env bash
# Better Captions installer with the plugin's debug log turned on, for macOS and Linux.
#
#   curl -fsSL https://raw.githubusercontent.com/anthonymendez/DavinciResolveBetterTranscription/main/install/install-debug.sh | bash
#
# Runs install.sh with BETTER_CAPTIONS_DEBUG=1; every other BETTER_CAPTIONS_* variable and
# argument is passed through. Each run in Resolve then writes <data dir>/logs/better-captions-*.log.
# Re-run the normal install.sh to turn the log off again.

# Everything runs from main() at the bottom, so a truncated download via
# `curl | bash` never runs half a script.

set -euo pipefail

REPO="anthonymendez/DavinciResolveBetterTranscription"

main() {
    local here="" tmp
    export BETTER_CAPTIONS_DEBUG=1
    # Run from a local copy: use the install.sh next to this file.
    if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
        here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
    fi
    if [ -n "$here" ] && [ -f "$here/install.sh" ]; then
        exec bash "$here/install.sh" "$@"
    fi
    # Piped from curl: fetch install.sh, then run it from a file so its prompts can read the terminal.
    tmp="$(mktemp "${TMPDIR:-/tmp}/better-captions-install.XXXXXX")"
    trap 'rm -f "$tmp"' EXIT
    curl -fsSL "https://raw.githubusercontent.com/$REPO/main/install/install.sh" -o "$tmp" ||
        {
            printf 'error: could not download install.sh\n' >&2
            exit 1
        }
    bash "$tmp" "$@"
}

main "$@"
