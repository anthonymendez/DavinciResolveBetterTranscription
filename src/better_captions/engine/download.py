"""Model and binary downloads shared by the engine backends."""

from __future__ import annotations

import hashlib
import os
import urllib.request
from pathlib import Path


def download(url: str, dest: Path, sha256: str | None = None) -> None:
    """Download url to dest via a .part file; verify sha256 when given (mismatch removes the file)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    digest = hashlib.sha256()
    try:
        with urllib.request.urlopen(url, timeout=60) as resp, open(tmp, "wb") as f:
            while chunk := resp.read(1 << 20):
                digest.update(chunk)
                f.write(chunk)
    except Exception as e:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(
            f"could not download {url} to {dest}: {e} (download it there by hand to work offline)"
        ) from e
    if sha256 is not None and digest.hexdigest() != sha256:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"checksum mismatch for {url}: expected sha256 {sha256}, got {digest.hexdigest()}")
    os.replace(tmp, dest)
