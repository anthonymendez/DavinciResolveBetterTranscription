"""On-disk cache of the expensive stages (transcription words, diarization turns).

Entries are JSON files under ``<data dir>/cache``, keyed by the SHA-256 of the audio
file's content plus the settings that stage depends on. Words and turns are stored
separately, so changing only the speaker count re-runs diarization but reuses the
transcription. Total size is capped; least-recently-used entries (by file mtime,
bumped on every hit) are evicted first.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import asdict
from pathlib import Path

from better_captions import paths
from better_captions.schema import SpeakerTurn, Word

DEFAULT_MAX_BYTES = 2 * 1024**3
FORMAT_VERSION = 1  # bump to invalidate every existing entry
_CHUNK = 1 << 20


def cache_dir() -> Path:
    return paths.data_dir() / "cache"


def file_digest(path: str | Path) -> str:
    """SHA-256 of a file's content, read in chunks so long recordings aren't loaded whole."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def _key(kind: str, audio_digest: str, settings: dict) -> str:
    blob = json.dumps({"v": FORMAT_VERSION, "kind": kind, "audio": audio_digest, "settings": settings}, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def transcription_key(audio_digest: str, *, model: str, language: str | None, backend: str) -> str:
    return _key("words", audio_digest, {"model": model, "language": language, "backend": backend})


def diarization_key(
    audio_digest: str,
    *,
    num_speakers: int | None,
    min_speakers: int | None,
    max_speakers: int | None,
    backend: str,
) -> str:
    settings = {"num": num_speakers, "min": min_speakers, "max": max_speakers, "backend": backend}
    return _key("turns", audio_digest, settings)


class TranscriptCache:
    def __init__(self, root: str | Path | None = None, max_bytes: int = DEFAULT_MAX_BYTES):
        self.root = Path(root) if root is not None else cache_dir()
        self.max_bytes = max_bytes

    def _path(self, kind: str, key: str) -> Path:
        return self.root / f"{kind}-{key}.json"

    def _read(self, kind: str, key: str) -> dict | None:
        path = self._path(kind, key)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            data = None
        if not isinstance(data, dict):
            _unlink(path)
            return None
        try:
            os.utime(path)  # mark as recently used
        except OSError:
            pass
        return data

    def _write(self, kind: str, key: str, data: dict) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        path = self._path(kind, key)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, path)
        self.evict()

    def get_words(self, key: str) -> tuple[str, float, list[Word]] | None:
        data = self._read("words", key)
        try:
            return data["language"], float(data["duration"]), [Word(**w) for w in data["words"]]
        except (TypeError, KeyError, ValueError):
            if data is not None:
                _unlink(self._path("words", key))
            return None

    def put_words(self, key: str, language: str, duration: float, words: list[Word]) -> None:
        self._write("words", key, {"language": language, "duration": duration, "words": [asdict(w) for w in words]})

    def get_turns(self, key: str) -> list[SpeakerTurn] | None:
        data = self._read("turns", key)
        try:
            return [SpeakerTurn(**t) for t in data["turns"]]
        except (TypeError, KeyError, ValueError):
            if data is not None:
                _unlink(self._path("turns", key))
            return None

    def put_turns(self, key: str, turns: list[SpeakerTurn]) -> None:
        self._write("turns", key, {"turns": [asdict(t) for t in turns]})

    def _entries(self) -> list[tuple[float, int, Path]]:
        out = []
        for p in self.root.glob("*.json"):
            try:
                st = p.stat()
            except OSError:
                continue
            out.append((st.st_mtime, st.st_size, p))
        return out

    def evict(self) -> None:
        """Delete least-recently-used entries until the cache fits in ``max_bytes``."""
        entries = sorted(self._entries(), key=lambda e: e[0])
        total = sum(size for _, size, _ in entries)
        for _, size, p in entries:
            if total <= self.max_bytes:
                break
            _unlink(p)
            total -= size

    def clear(self) -> int:
        """Delete every entry; returns how many were removed."""
        if not self.root.is_dir():
            return 0
        removed = 0
        for p in list(self.root.glob("*.json")) + list(self.root.glob("*.tmp")):
            if _unlink(p):
                removed += 1
        return removed


def clear(root: str | Path | None = None) -> int:
    """Empty the cache (default: ``<data dir>/cache``); returns how many entries were removed."""
    return TranscriptCache(root).clear()


def _unlink(path: Path) -> bool:
    try:
        path.unlink()
        return True
    except OSError:
        return False
