#!/usr/bin/env bash
# Better Captions installer with the plugin's debug log turned on, for macOS and Linux.
#
#   curl -fsSL https://raw.githubusercontent.com/anthonymendez/DavinciResolveBetterTranscription/main/install/install-debug.sh | bash
#   bash install/install-debug.sh                (in a clone or extracted zip: installs that copy)
#
# Options, so no environment variable has to be set by hand (any BETTER_CAPTIONS_* variable that is
# set still applies; an option wins over it):
#   --ref REF       install this branch, tag or commit from GitHub (e.g. --ref main)
#   --source DIR    install from this local copy (the folder with pyproject.toml)
#   --github        install from GitHub even when run from a local copy
# With none of them, a run from a local copy installs that copy, and a run piped from curl installs the
# latest release from GitHub. Each run in Resolve then writes <data dir>/logs/better-captions-*.log.
# Re-run the normal install.sh to turn the log off again.

# Everything runs from main() at the bottom, so a truncated download via
# `curl | bash` never runs half a script.

set -euo pipefail

REPO="anthonymendez/DavinciResolveBetterTranscription"

usage() {
    sed -n '2,15p' "${BASH_SOURCE[0]:-/dev/null}" 2>/dev/null | sed 's/^# \{0,1\}//' ||
        printf 'usage: install-debug.sh [--ref REF] [--source DIR] [--github]\n'
}

die() {
    printf 'error: %s\n' "$*" >&2
    exit 1
}

main() {
    local here="" root="" ref="" source="" github=0 tmp
    while [ $# -gt 0 ]; do
        case "$1" in
            --ref)
                [ $# -ge 2 ] || die "--ref needs a branch, tag or commit"
                ref="$2"
                shift 2
                ;;
            --ref=*)
                ref="${1#--ref=}"
                shift
                ;;
            --source)
                [ $# -ge 2 ] || die "--source needs a folder"
                source="$2"
                shift 2
                ;;
            --source=*)
                source="${1#--source=}"
                shift
                ;;
            --github)
                github=1
                shift
                ;;
            -h | --help)
                usage
                return 0
                ;;
            *) die "unknown option: $1 (see --help)" ;;
        esac
    done
    if [ -n "$source" ] && { [ -n "$ref" ] || [ "$github" = 1 ]; }; then
        die "--source installs a local copy; it can't be combined with --ref or --github"
    fi

    # Run from a file: the install.sh next to it, and the copy it sits in (if it is one).
    if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
        here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
        [ -f "$here/../pyproject.toml" ] && root="$(cd "$here/.." && pwd)"
    fi

    export BETTER_CAPTIONS_DEBUG=1
    if [ -n "$source" ]; then
        export BETTER_CAPTIONS_SOURCE="$source"
    elif [ -n "$ref" ] || [ "$github" = 1 ]; then
        unset BETTER_CAPTIONS_SOURCE
    elif [ -z "${BETTER_CAPTIONS_SOURCE:-}" ] && [ -n "$root" ]; then
        export BETTER_CAPTIONS_SOURCE="$root"
    fi
    if [ -n "$ref" ]; then
        export BETTER_CAPTIONS_REF="$ref"
    fi

    if [ -n "$here" ] && [ -f "$here/install.sh" ]; then
        exec bash "$here/install.sh"
    fi
    # Piped from curl: fetch install.sh, then run it from a file so its prompts can read the terminal.
    tmp="$(mktemp "${TMPDIR:-/tmp}/better-captions-install.XXXXXX")"
    trap 'rm -f "$tmp"' EXIT
    curl -fsSL "https://raw.githubusercontent.com/$REPO/main/install/install.sh" -o "$tmp" ||
        die "could not download install.sh"
    bash "$tmp"
}

main "$@"
