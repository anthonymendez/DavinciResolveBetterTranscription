#!/usr/bin/env bash
# Better Captions installer for macOS and Linux.
#
#   curl -fsSL https://raw.githubusercontent.com/anthonymendez/DavinciResolveBetterTranscription/main/install/install.sh | bash
#
# Re-running it upgrades an existing install. Never needs sudo, except to
# install python.org's Python on macOS when Resolve has none (asks first).
# Install layout: see docs/ARCHITECTURE.md ("Install layout").
#
# Environment:
#   BETTER_CAPTIONS_HOME       data dir override (default: per-OS, see below)
#   BETTER_CAPTIONS_REF        git ref to install from GitHub (default: main)
#   BETTER_CAPTIONS_SOURCE     install from this local source dir instead of GitHub (dev/tests)
#   BETTER_CAPTIONS_SKIP_DEPS  =1: install the package with --no-deps (tests only)
#   BETTER_CAPTIONS_GPU        auto (default): install the NVIDIA CUDA runtime when nvidia-smi finds a GPU
#                              (Linux); cuda: always install it; none: never (see docs/GPU.md)
#   BETTER_CAPTIONS_SKIP_SYSTEM_PYTHON  =1: don't check/install the Python Resolve runs the plugin with
#   BETTER_CAPTIONS_RESOLVE_PYTHONS     colon-separated Pythons to check instead of Resolve's locations (tests)
#   BETTER_CAPTIONS_NONINTERACTIVE      =1: never prompt (tests)
#   BETTER_CAPTIONS_LIB        =1: only define the functions, don't run main (tests)

# Everything runs from main() at the bottom, so a truncated download via
# `curl | bash` never runs half a script.

set -euo pipefail

REPO="anthonymendez/DavinciResolveBetterTranscription"
PYTHON_VERSION="3.12"
# Last 3.12 release with python.org installers.
RESOLVE_PYTHON_RELEASE="3.12.10"

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

# ---------------------------------------------------------------------------
# NVIDIA GPU
# faster-whisper runs on NVIDIA GPUs with the CUDA 12 cuBLAS + cuDNN 9
# runtime, installed from pip as the package's `cuda` extra. macOS has no
# CUDA. See docs/GPU.md.
# ---------------------------------------------------------------------------

# True when nvidia-smi (installed with the NVIDIA driver) lists a GPU.
has_nvidia_gpu() {
    local out
    command -v nvidia-smi >/dev/null 2>&1 || return 1
    out="$(nvidia-smi -L 2>/dev/null)" || return 1
    [[ "$out" == *"GPU "[0-9]* ]]
}

# Prints "cuda" or "none": whether to install the CUDA runtime.
# BETTER_CAPTIONS_GPU=auto|cuda|none overrides the detection.
gpu_choice() {
    local os="$1" choice="${BETTER_CAPTIONS_GPU:-auto}"
    case "$choice" in
        cuda | none) ;;
        auto)
            if [ "$os" = linux ] && has_nvidia_gpu; then
                choice=cuda
            else
                choice=none
            fi
            ;;
        *) die "BETTER_CAPTIONS_GPU must be auto, cuda or none (got '$choice')" ;;
    esac
    if [ "$choice" = cuda ] && [ "$os" = macos ]; then
        warn "BETTER_CAPTIONS_GPU=cuda: CUDA isn't available on macOS; installing without it"
        choice=none
    fi
    echo "$choice"
}

install_package() {
    local data="$1" src="$2" gpu="$3" py target
    py="$(venv_python "$data")"
    local args=(pip install --no-config --python "$py" --reinstall-package better-captions)
    if [ "${BETTER_CAPTIONS_SKIP_DEPS:-}" = 1 ]; then
        args+=(--no-deps)
    fi
    target="$src"
    if [ "$gpu" = cuda ]; then
        target="$src[cuda]"
        say "NVIDIA GPU: also installing its CUDA runtime (cuBLAS + cuDNN, about 1.5 GB)"
    fi
    say "Installing the Better Captions engine (this can take a few minutes the first time)"
    uv "${args[@]}" "$target" || die "package install failed"
}

# Writes config.json, keeping any user settings already in it.
write_config() {
    local data="$1" gpu="$2" py
    py="$(venv_python "$data")"
    BC_CONFIG="$data/config.json" BC_PYTHON="$py" BC_GPU="$gpu" "$py" - <<'PY' || die "could not write config.json"
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
config["gpu"] = os.environ["BC_GPU"]
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
# RESOLVE SYSTEM PYTHON
# Resolve runs the plugin with a system Python it finds by itself: a 64-bit
# CPython 3.10-3.12 with tkinter, from python.org on macOS
# (/Library/Frameworks/Python.framework) or the distro's /usr/bin/python3 on
# Linux. See docs/RESOLVE_INTEGRATION.md ("Installer requirements").
# ---------------------------------------------------------------------------

# Where Resolve looks for Python, best first. BETTER_CAPTIONS_RESOLVE_PYTHONS
# (colon-separated paths) replaces the list (tests).
resolve_python_candidates() {
    if [ -n "${BETTER_CAPTIONS_RESOLVE_PYTHONS:-}" ]; then
        printf '%s\n' "$BETTER_CAPTIONS_RESOLVE_PYTHONS" | tr ':' '\n'
    elif [ "$1" = macos ]; then
        local v
        for v in 3.12 3.11 3.10; do
            echo "/Library/Frameworks/Python.framework/Versions/$v/bin/python3"
        done
        echo /usr/local/bin/python3
    else
        echo /usr/bin/python3
        echo /usr/local/bin/python3
    fi
}

# Prints "<path> <major.minor>" for the first candidate that is a 64-bit
# Python 3.10-3.12 with tkinter; fails if there is none.
find_resolve_python() {
    local py info version bits
    while IFS= read -r py; do
        [ -n "$py" ] && [ -x "$py" ] || continue
        info="$("$py" -c "import struct, sys, tkinter; print('%d.%d %d' % (sys.version_info[:2] + (struct.calcsize('P') * 8,)))" 2>/dev/null)" ||
            continue
        version="${info% *}"
        bits="${info#* }"
        case "$version" in
            3.10 | 3.11 | 3.12) ;;
            *) continue ;;
        esac
        [ "$bits" = 64 ] || continue
        echo "$py $version"
        return 0
    done < <(resolve_python_candidates "$1")
    return 1
}

# The exact command that installs python3 + tkinter with this distro's
# package manager (all of them if it can't tell).
linux_python_hint() {
    if command -v apt-get >/dev/null 2>&1; then
        echo "  sudo apt-get install python3 python3-tk"
    elif command -v dnf >/dev/null 2>&1; then
        echo "  sudo dnf install python3 python3-tkinter"
    elif command -v pacman >/dev/null 2>&1; then
        echo "  sudo pacman -S python tk"
    else
        echo "  Debian/Ubuntu:  sudo apt-get install python3 python3-tk"
        echo "  Fedora:         sudo dnf install python3 python3-tkinter"
        echo "  Arch:           sudo pacman -S python tk"
    fi
}

# True when we can ask the user a question (even under `curl | bash`).
can_prompt() {
    [ "${BETTER_CAPTIONS_NONINTERACTIVE:-}" != 1 ] && { : </dev/tty; } 2>/dev/null
}

install_macos_python() {
    local workdir="$1" pkg answer
    local url="https://www.python.org/ftp/python/$RESOLVE_PYTHON_RELEASE/python-$RESOLVE_PYTHON_RELEASE-macos11.pkg"
    if ! can_prompt; then
        warn "not running interactively, so Python was not installed. Install Python $PYTHON_VERSION from"
        warn "python.org ($url), or re-run this installer in a terminal."
        return 1
    fi
    printf '\nResolve needs Python %s from python.org to run the plugin.\n' "$PYTHON_VERSION" >/dev/tty
    printf 'Install Python %s now? This downloads the official installer and runs\n' "$RESOLVE_PYTHON_RELEASE" >/dev/tty
    printf '`sudo installer`, which asks for your password. [y/N] ' >/dev/tty
    read -r answer </dev/tty || answer=""
    case "$answer" in
        y | Y | yes | YES) ;;
        *)
            warn "skipped installing Python. Install Python $PYTHON_VERSION from $url before using the plugin."
            return 1
            ;;
    esac
    pkg="$workdir/python-$RESOLVE_PYTHON_RELEASE.pkg"
    say "Downloading $url"
    curl -fsSL "$url" -o "$pkg" || {
        warn "could not download $url"
        return 1
    }
    say "Installing Python $RESOLVE_PYTHON_RELEASE (sudo installer)"
    sudo installer -pkg "$pkg" -target / || {
        warn "the Python installer failed"
        return 1
    }
}

ensure_resolve_python() {
    local os="$1" workdir="$2" found
    if [ "${BETTER_CAPTIONS_SKIP_SYSTEM_PYTHON:-}" = 1 ]; then
        say "Skipping the system Python check for Resolve (BETTER_CAPTIONS_SKIP_SYSTEM_PYTHON=1)"
        return
    fi
    if found="$(find_resolve_python "$os")"; then
        say "Resolve will use Python ${found##* } at ${found% *}"
        return
    fi
    if [ "$os" = linux ]; then
        warn "Resolve needs a 64-bit Python 3.10-3.12 with tkinter at /usr/bin/python3 to run the plugin."
        warn "This installer doesn't install system packages; install them with:"
        linux_python_hint >&2
        return
    fi
    install_macos_python "$workdir" || return 0
    if found="$(find_resolve_python "$os")"; then
        say "Installed Python ${found##* } at ${found% *}; restart DaVinci Resolve to pick it up"
    else
        warn "Python was installed but Resolve's Python is still not found; see docs/RESOLVE_INTEGRATION.md"
    fi
}

print_next_steps() {
    local data="$1" gpu="$2" gpu_note
    if [ "$gpu" = cuda ]; then
        gpu_note="The NVIDIA GPU runtime (CUDA) is installed; the engine uses your GPU
automatically and falls back to the CPU if it can't."
    else
        gpu_note="No NVIDIA GPU was detected, so the CUDA runtime was not installed. To
install it anyway, re-run with BETTER_CAPTIONS_GPU=cuda."
    fi
    cat <<EOF

Better Captions is installed.
  Engine:  $data/venv
  Config:  $data/config.json

Next steps:
  1. Restart DaVinci Resolve if it is running.
  2. Open Workspace > Scripts > Better Captions.

$gpu_note
Check what the engine detects: "$(venv_python "$data")" -m better_captions.cli doctor
Re-run this installer any time to upgrade.
EOF
}

main() {
    local os data src gpu
    os="$(detect_os)"
    gpu="$(gpu_choice "$os")" || exit 1
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
    install_package "$data" "$src" "$gpu"
    write_config "$data" "$gpu"
    install_plugin "$os" "$data" "$src"
    ensure_resolve_python "$os" "$workdir"
    print_next_steps "$data" "$gpu"
}

if [ "${BETTER_CAPTIONS_LIB:-}" != 1 ]; then
    main "$@"
fi
