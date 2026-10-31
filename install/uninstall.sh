#!/usr/bin/env bash
# Better Captions uninstaller for macOS and Linux.
#
#   curl -fsSL https://raw.githubusercontent.com/anthonymendez/DavinciResolveBetterTranscription/main/install/uninstall.sh | bash
#
# Removes the engine venv, config.json, the debug logs and the Resolve plugin files the
# installer copied. Leaves uv installed. Honours BETTER_CAPTIONS_HOME.

set -euo pipefail

say() { printf '==> %s\n' "$*"; }
die() {
    printf 'error: %s\n' "$*" >&2
    exit 1
}

detect_os() {
    case "$(uname -s)" in
        Darwin) echo macos ;;
        Linux) echo linux ;;
        *) die "unsupported OS '$(uname -s)'. On Windows use uninstall.ps1." ;;
    esac
}

data_dir() {
    if [ -n "${BETTER_CAPTIONS_HOME:-}" ]; then
        echo "$BETTER_CAPTIONS_HOME"
    elif [ "$1" = macos ]; then
        echo "$HOME/Library/Application Support/BetterCaptions"
    else
        echo "${XDG_DATA_HOME:-$HOME/.local/share}/BetterCaptions"
    fi
}

resolve_utility_dir() {
    if [ "$1" = macos ]; then
        echo "$HOME/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts/Utility"
    else
        echo "$HOME/.local/share/DaVinciResolve/Fusion/Scripts/Utility"
    fi
}

main() {
    local os data dest manifest entry
    os="$(detect_os)"
    data="$(data_dir "$os")"
    dest="$(resolve_utility_dir "$os")"
    manifest="$data/plugin_files.txt"

    if [ -f "$manifest" ]; then
        while IFS= read -r entry; do
            [ -n "$entry" ] && rm -rf "${dest:?}/$entry"
        done <"$manifest"
        say "Removed the Resolve plugin from $dest"
    fi

    if [ ! -d "$data" ]; then
        say "Nothing installed at $data"
        return
    fi
    # Only remove what the installer created: BETTER_CAPTIONS_HOME may point
    # at a directory that holds other things.
    rm -rf "${data:?}/venv" "$data/config.json" "$data/config.json.tmp" "$manifest"
    # The plugin's debug logs (install-debug.sh).
    rm -f "$data/logs"/better-captions-*.log
    rmdir "$data/logs" 2>/dev/null || true
    rmdir "$data" 2>/dev/null || say "Left $data in place (it still contains other files)"
    say "Better Captions has been uninstalled."
}

main "$@"
