"""Checks that a release tag matches the package version and prints the version.

    python .github/scripts/check_release_version.py v0.2.0

pyproject.toml's [project] version is the single source of truth. The Resolve
plugin can't read package metadata (it runs on Resolve's Python and never
imports the engine), so its hard-coded __version__ must match it too.
Exits non-zero with a message saying what to fix. See docs/RELEASING.md.
"""

import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLUGIN_INIT = ROOT / "resolve_plugin" / "src" / "better_captions_plugin" / "__init__.py"


def project_version() -> str:
    with open(ROOT / "pyproject.toml", "rb") as f:
        return tomllib.load(f)["project"]["version"]


def plugin_version() -> str | None:
    match = re.search(r'^__version__ = "([^"]+)"', PLUGIN_INIT.read_text(encoding="utf-8"), re.MULTILINE)
    return match.group(1) if match else None


def check(tag: str) -> list[str]:
    """The problems with releasing `tag`, or [] if it's good to go."""
    version = project_version()
    problems = []
    if not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
        problems.append(f"tag {tag!r} is not of the form vMAJOR.MINOR.PATCH")
    if tag != f"v{version}":
        problems.append(f"tag {tag!r} does not match pyproject.toml's version {version!r} (expected 'v{version}')")
    plugin = plugin_version()
    if plugin != version:
        problems.append(f"{PLUGIN_INIT.relative_to(ROOT)} has __version__ {plugin!r}, expected {version!r}")
    return problems


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: check_release_version.py <tag>", file=sys.stderr)
        return 2
    problems = check(argv[0])
    for problem in problems:
        print(f"error: {problem}", file=sys.stderr)
    if problems:
        return 1
    print(project_version())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
