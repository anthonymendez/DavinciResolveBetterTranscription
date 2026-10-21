#!/usr/bin/env bash
# Better Captions installer for macOS and Linux.
#
#   curl -fsSL https://raw.githubusercontent.com/anthonymendez/DavinciResolveBetterTranscription/main/install/install.sh | bash
#
# Re-running it upgrades an existing install. Never needs sudo.
# Install layout: see docs/ARCHITECTURE.md ("Install layout").
#
# Environment:
#   BETTER_CAPTIONS_HOME       data dir override (default: per-OS, see below)
#   BETTER_CAPTIONS_REF        git ref to install from GitHub (default: main)
#   BETTER_CAPTIONS_SOURCE     install from this local source dir instead of GitHub (dev/tests)
#   BETTER_CAPTIONS_SKIP_DEPS  =1: install the package with --no-deps (tests only)

# Everything runs from main() at the bottom, so a truncated download via
# `curl | bash` never runs half a script.

set -euo pipefail

REPO="anthonymendez/DavinciResolveBetterTranscription"
PYTHON_VERSION="3.12"

say() { printf '==> %s\n' "$*"; }
warn() { printf 'warning: %s\n' "$*" >&2; }
die() {
    printf 'error: %s\n' "$*" >&2
    exit 1
}

detect_os() {
    case "$(uname -s)" in
        Darwin) echo macos ;;
        Linux) echo linux ;;
        *) die "unsupported OS '$(uname -s)'. On Windows use install.ps1." ;;
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

resolve_installed() {
    if [ "$1" = macos ]; then
        [ -d "/Applications/DaVinci Resolve" ] ||
            [ -d "$HOME/Library/Application Support/Blackmagic Design/DaVinci Resolve" ]
    else
        [ -d /opt/resolve ] || [ -d "$HOME/.local/share/DaVinciResolve" ]
    fi
}

ensure_uv() {
    if command -v uv >/dev/null 2>&1; then
        return
    fi
    # The official installer puts uv in ~/.local/bin (no admin needed).
    export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
    if command -v uv >/dev/null 2>&1; then
        return
    fi
    command -v curl >/dev/null 2>&1 || die "curl is required to install uv"
    say "Installing uv (Python package manager) into ~/.local/bin"
    curl -LsSf https://astral.sh/uv/install.sh | sh ||
        die "uv install failed; see https://docs.astral.sh/uv/getting-started/installation/"
    command -v uv >/dev/null 2>&1 || die "uv was installed but is not on PATH; open a new terminal and re-run"
}

# Prints the source dir to install from. Downloads into $1 unless
# BETTER_CAPTIONS_SOURCE is set.
fetch_source() {
    local workdir="$1"
    if [ -n "${BETTER_CAPTIONS_SOURCE:-}" ]; then
        [ -f "$BETTER_CAPTIONS_SOURCE/pyproject.toml" ] ||
            die "BETTER_CAPTIONS_SOURCE=$BETTER_CAPTIONS_SOURCE has no pyproject.toml"
        (cd "$BETTER_CAPTIONS_SOURCE" && pwd)
        return
    fi
    local ref="${BETTER_CAPTIONS_REF:-main}"
    local url="https://github.com/$REPO/archive/$ref.tar.gz"
    say "Downloading $REPO@$ref" >&2
    curl -fsSL "$url" -o "$workdir/src.tar.gz" ||
        die "could not download $url (check BETTER_CAPTIONS_REF and your network)"
    mkdir -p "$workdir/src"
    tar -xzf "$workdir/src.tar.gz" -C "$workdir/src" --strip-components=1 ||
        die "could not unpack $url"
    echo "$workdir/src"
}

venv_python() {
    echo "$1/venv/bin/python"
}

create_venv() {
    local data="$1" py
    py="$(venv_python "$data")"
    if [ -x "$py" ] && "$py" -c "import sys; sys.exit(sys.version_info[:2] != (3, 12))" 2>/dev/null; then
        say "Reusing engine venv at $data/venv"
        return
    fi
    say "Creating engine venv (Python $PYTHON_VERSION) at $data/venv"
    uv venv --no-project --no-config --clear --managed-python --python "$PYTHON_VERSION" "$data/venv" ||
        die "could not create the venv at $data/venv"
}

install_package() {
    local data="$1" src="$2" py
    py="$(venv_python "$data")"
    local args=(pip install --no-config --python "$py" --reinstall-package better-captions)
    if [ "${BETTER_CAPTIONS_SKIP_DEPS:-}" = 1 ]; then
        args+=(--no-deps)
    fi
    say "Installing the Better Captions engine (this can take a few minutes the first time)"
    uv "${args[@]}" "$src" || die "package install failed"
}

# Writes config.json, keeping any user settings already in it.
write_config() {
    local data="$1" py
    py="$(venv_python "$data")"
    BC_CONFIG="$data/config.json" BC_PYTHON="$py" "$py" - <<'PY' || die "could not write config.json"
import importlib.metadata
import json
import os

path = os.environ["BC_CONFIG"]
config = {}
if os.path.exists(path):
    try:
        with open(path, encoding="utf-8") as f:
            config = json.load(f)
    except (OSError, ValueError):
        config = {}
config["engine"] = [os.environ["BC_PYTHON"], "-m", "better_captions.cli"]
config["version"] = importlib.metadata.version("better-captions")
tmp = path + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    json.dump(config, f, indent=2)
    f.write("\n")
os.replace(tmp, path)
PY
}

# Copies resolve_plugin/Utility/* into Resolve's Scripts/Utility and records
# what was copied in <data>/plugin_files.txt so upgrades and uninstall can
# remove exactly those entries.
install_plugin() {
    local os="$1" data="$2" src="$3" dest manifest
    dest="$(resolve_utility_dir "$os")"
    manifest="$data/plugin_files.txt"
    if ! resolve_installed "$os"; then
        warn "DaVinci Resolve was not found; installing the plugin anyway. Install Resolve to use it."
    fi
    if [ ! -d "$src/resolve_plugin/Utility" ]; then
        warn "no resolve_plugin/Utility in the source; skipping the Resolve plugin"
        return
    fi
    mkdir -p "$dest" || die "could not create $dest"
    if [ -f "$manifest" ]; then
        local old
        while IFS= read -r old; do
            [ -n "$old" ] && rm -rf "${dest:?}/$old"
        done <"$manifest"
    fi
    : >"$manifest"
    local entry
    for entry in "$src/resolve_plugin/Utility"/* "$src/resolve_plugin/Utility"/.[!.]*; do
        [ -e "$entry" ] || continue
        cp -R "$entry" "$dest/" || die "could not copy $entry to $dest"
        basename "$entry" >>"$manifest"
    done
    say "Installed the Resolve plugin into $dest"
}

# ---------------------------------------------------------------------------
# RESOLVE SYSTEM PYTHON (placeholder)
# If the Resolve plugin turns out to need a system Python for Resolve's own
# scripting host (Workspace > Scripts runs .py scripts with an external
# Python 3), detect/install it here and point Resolve at it. Currently a
# no-op: the plugin talks to the engine through config.json["engine"].
# ---------------------------------------------------------------------------
ensure_resolve_python() {
    :
}

print_next_steps() {
    local data="$1"
    cat <<EOF

Better Captions is installed.
  Engine:  $data/venv
  Config:  $data/config.json

Next steps:
  1. Restart DaVinci Resolve if it is running.
  2. Open Workspace > Scripts > Better Captions.

An NVIDIA GPU with CUDA is used automatically if available; otherwise the
engine runs on the CPU.
Re-run this installer any time to upgrade.
EOF
}

main() {
    local os data src
    os="$(detect_os)"
    data="$(data_dir "$os")"
    if [ "$(id -u)" = 0 ] && [ -z "${BETTER_CAPTIONS_HOME:-}" ]; then
        warn "running as root installs for root only; run as your normal user instead (no sudo needed)"
    fi
    mkdir -p "$data" || die "could not create $data"
    ensure_uv
    # Global, so the EXIT trap can still see it after main returns.
    workdir="$(mktemp -d "${TMPDIR:-/tmp}/better-captions.XXXXXX")"
    trap 'rm -rf "$workdir"' EXIT
    src="$(fetch_source "$workdir")"
    create_venv "$data"
    install_package "$data" "$src"
    write_config "$data"
    install_plugin "$os" "$data" "$src"
    ensure_resolve_python
    print_next_steps "$data"
}

main "$@"
