"""Speaker-aware transcription and captioning for DaVinci Resolve."""

from importlib.metadata import PackageNotFoundError, version

# pyproject.toml's version is the single source of truth; see docs/RELEASING.md.
try:
    __version__ = version("better-captions")
except PackageNotFoundError:  # running from a source tree that isn't installed
    __version__ = "0+unknown"
