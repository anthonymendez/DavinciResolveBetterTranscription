"""Which Resolve is running, and what it lets a Python script do.

Resolve 21.1 (September 2026) moved Python scripting to the Studio edition: on the free
edition a Python script gets no usable ``resolve`` object, or one whose calls return None
(see docs/RESOLVE_INTEGRATION.md §9). Lua scripting and Studio are unaffected. This module
reads the version and edition so the plugin can say so instead of failing with
"open a project first".
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# First release whose free edition no longer runs Python scripts against the API.
PYTHON_STUDIO_ONLY_SINCE = (21, 1)
# Newest major.minor the plugin's API calls have been checked against (docs/RESOLVE_INTEGRATION.md).
TESTED_UP_TO = (21, 1)

_NUMBER = re.compile(r"\d+")


@dataclass(frozen=True)
class ResolveInfo:
    version: tuple  # (major, minor, patch, build); () when unknown
    product: str = ""
    studio: bool = False

    @property
    def version_text(self) -> str:
        return ".".join(str(part) for part in self.version[:3]) if self.version else "unknown version"

    @property
    def label(self) -> str:
        return "%s %s" % (self.product or "DaVinci Resolve", self.version_text)

    @property
    def python_gated(self) -> bool:
        """True when this edition/version does not give Python scripts the API (free 21.1+)."""
        return not self.studio and bool(self.version) and self.version[:2] >= PYTHON_STUDIO_ONLY_SINCE

    @property
    def newer_than_tested(self) -> bool:
        return bool(self.version) and self.version[:2] > TESTED_UP_TO


def parse_version(value) -> tuple:
    """``GetVersion()``'s ``[21, 1, 2, 5, ""]`` or ``GetVersionString()``'s ``"21.1.2.0005"`` /
    ``"21.1.2 Build 5"`` -> ``(21, 1, 2, 5)``. Unparseable values give ``()``."""
    if value is None:
        return ()
    if isinstance(value, dict):  # Lua tables come through the bridge as {1: 21, 2: 1, ...}
        value = [value[k] for k in sorted(value) if isinstance(k, int)]
    if isinstance(value, (list, tuple)):
        parts = []
        for part in value:
            if isinstance(part, bool):
                break
            if isinstance(part, int):
                parts.append(part)
            elif (isinstance(part, float) and part.is_integer()) or (isinstance(part, str) and part.strip().isdigit()):
                parts.append(int(part))
            else:
                break  # the trailing suffix ("", "b", ...)
        return tuple(parts[:4])
    return tuple(int(n) for n in _NUMBER.findall(str(value))[:4])


def detect(resolve) -> ResolveInfo:
    """Read the version and edition; any call that fails just leaves that part unknown."""
    if resolve is None:
        return ResolveInfo(version=())
    version = ()
    for getter in ("GetVersion", "GetVersionString"):
        try:
            version = parse_version(getattr(resolve, getter)())
        except Exception:
            version = ()
        if version:
            break
    try:
        product = str(resolve.GetProductName() or "")
    except Exception:
        product = ""
    return ResolveInfo(version=version, product=product, studio="studio" in product.lower())


NO_RESOLVE_MESSAGE = (
    "Resolve did not give this script access to its scripting API.\n\n"
    "Since DaVinci Resolve 21.1, Python scripts only work in DaVinci Resolve Studio; the free "
    "edition no longer runs them. If you are on Resolve Studio, check that you started Better "
    "Captions from Workspace > Scripts inside Resolve."
)


def gated_message(info: ResolveInfo) -> str:
    return (
        "%s does not let Python scripts use its scripting API: since Resolve 21.1, Python scripting "
        "needs DaVinci Resolve Studio.\n\n"
        "Better Captions needs Resolve Studio 21.1 or newer, or the free edition of Resolve 21.0.x or older."
        % info.label
    )


def warnings_for(info: ResolveInfo) -> list[str]:
    """Notes for the summary dialog; empty for a version the plugin is tested against."""
    if info.newer_than_tested:
        return [
            "%s is newer than the versions Better Captions was tested with (up to %d.%d); "
            "please report anything that looks wrong." % (info.label, TESTED_UP_TO[0], TESTED_UP_TO[1])
        ]
    return []
