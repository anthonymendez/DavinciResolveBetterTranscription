"""Pack resolve_plugin/src into resolve_plugin/Utility/better_captions_plugin.zip.

Run after editing anything under resolve_plugin/src:

    uv run python resolve_plugin/build_plugin_zip.py

The zip is deterministic (sorted entries, fixed timestamps), so rebuilding without source
changes is a no-op for git. tests/resolve/test_plugin_zip.py fails when the zip is stale.
"""

from __future__ import annotations

import io
import os
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "src")
ZIP = os.path.join(HERE, "Utility", "better_captions_plugin.zip")
PACKAGE = "better_captions_plugin"
FIXED_DATE = (1980, 1, 1, 0, 0, 0)


def source_files(src: str = SRC) -> "list[tuple[str, str]]":
    """``(archive name, absolute path)`` for every .py file of the package, sorted."""
    files = []
    root = os.path.join(src, PACKAGE)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
        for name in sorted(filenames):
            if name.endswith(".py"):
                full = os.path.join(dirpath, name)
                rel = os.path.relpath(full, src).replace(os.sep, "/")
                files.append((rel, full))
    return files


def build_bytes(src: str = SRC) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for rel, full in source_files(src):
            with open(full, "rb") as handle:
                data = handle.read().replace(b"\r\n", b"\n")
            info = zipfile.ZipInfo(rel, date_time=FIXED_DATE)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, data)
    return buffer.getvalue()


def is_up_to_date(zip_path: str = ZIP, src: str = SRC) -> bool:
    if not os.path.isfile(zip_path):
        return False
    # Compare entries, not raw bytes: deflate output differs between zlib builds (e.g. on Windows).
    def entries(archive: zipfile.ZipFile) -> "dict[str, bytes]":
        return {name: archive.read(name) for name in archive.namelist()}

    with zipfile.ZipFile(zip_path) as committed, zipfile.ZipFile(io.BytesIO(build_bytes(src))) as fresh:
        return entries(committed) == entries(fresh)


def build(zip_path: str = ZIP, src: str = SRC) -> bool:
    """Write the zip; return True when it changed."""
    data = build_bytes(src)
    if os.path.isfile(zip_path):
        with open(zip_path, "rb") as handle:
            if handle.read() == data:
                return False
    os.makedirs(os.path.dirname(zip_path), exist_ok=True)
    with open(zip_path, "wb") as handle:
        handle.write(data)
    return True


if __name__ == "__main__":
    changed = build()
    print("%s %s" % ("wrote" if changed else "unchanged", ZIP))
    sys.exit(0)
