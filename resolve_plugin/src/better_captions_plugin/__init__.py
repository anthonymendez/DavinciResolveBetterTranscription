"""Better Captions: the DaVinci Resolve side.

Runs inside Resolve (Workspace > Scripts > Better Captions) on whatever Python
Resolve's scripting host provides, so this package is standard library only and
never imports the ``better_captions`` engine. The engine is a subprocess; the
contract (CLI flags, JSON-line events, ``transcript.json``) is in
``docs/ARCHITECTURE.md`` and the Resolve API calls used are documented with
sources in ``docs/RESOLVE_INTEGRATION.md``.
"""

__version__ = "0.1.0"
