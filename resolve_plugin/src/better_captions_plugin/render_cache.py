"""Skip re-rendering the timeline audio when nothing that affects it changed since the last render.

``fingerprint`` hashes everything the audio render depends on, read through the Resolve API; ``lookup``
and ``store`` keep the finished renders in a small directory keyed by that hash. Stdlib only.

Compute the fingerprint while the track selection for the render is applied, and pass that selection as
``tracks``: Resolve's ``Timeline.GetIsTrackEnabled`` can keep reporting True right after
``SetTrackEnable(..., False)`` succeeded, so the enabled states it reports can't tell the full mix
apart from a one-track render.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil

# Renders kept in the cache directory; the least recently used beyond this are deleted on ``store``.
KEEP_ENTRIES = 5
_FINGERPRINT_LEN = 64  # sha256 hex digest


def fingerprint(timeline, range_start, range_end, clock, tracks=None) -> str:
    """Hex digest of the timeline state that determines the rendered audio for ``range_start``..``range_end``
    (``range_end`` None means "to the end"), with only the audio tracks in ``tracks`` enabled (None: the
    timeline as it is). Anything Resolve will not report hashes as ``None``, so an old or partial API
    still gives a stable fingerprint."""
    state = {
        "selection": None if tracks is None else sorted({int(i) for i in tracks}),
        "timeline": [_call(timeline, "GetUniqueId"), _call(timeline, "GetName")],
        "range": [_int(range_start), _int(range_end)],
        "clock": [clock.fps, bool(clock.drop_frame), _int(clock.start_frame)],
        "tracks": _audio_tracks(timeline),
    }
    payload = json.dumps(state, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def lookup(cache_dir: str, fp: str):
    """Path of the cached render for ``fp``, or None. A hit counts as use (it is pruned last)."""
    if not _valid(fp) or not os.path.isdir(cache_dir):
        return None
    for name in sorted(os.listdir(cache_dir)):
        path = os.path.join(cache_dir, name)
        if _entry_fp(name) == fp and os.path.isfile(path) and os.path.getsize(path) > 0:
            try:
                os.utime(path)
            except OSError:
                pass
            return path
    return None


def store(cache_dir: str, fp: str, wav: str, keep: int = KEEP_ENTRIES) -> str:
    """Copy the render at ``wav`` into the cache under ``fp`` and return the cached path; then drop the
    least recently used entries beyond ``keep``."""
    if not _valid(fp):
        raise ValueError("not a fingerprint: %r" % (fp,))
    os.makedirs(cache_dir, exist_ok=True)
    extension = os.path.splitext(wav)[1].lower() or ".wav"
    target = os.path.join(cache_dir, fp + extension)
    partial = target + ".part"
    shutil.copyfile(wav, partial)
    os.replace(partial, target)
    for name in os.listdir(cache_dir):  # the same render under another extension is stale
        if _entry_fp(name) == fp and os.path.join(cache_dir, name) != target:
            _remove(os.path.join(cache_dir, name))
    _prune(cache_dir, keep, target)
    return target


# ---------------------------------------------------------------------------
# Fingerprint inputs


def _audio_tracks(timeline) -> list:
    count = _int(_call(timeline, "GetTrackCount", "audio")) or 0
    tracks = []
    for index in range(1, count + 1):
        enabled = _call(timeline, "GetIsTrackEnabled", "audio", index)
        items = _call(timeline, "GetItemListInTrack", "audio", index) or []
        tracks.append(
            {
                "index": index,
                "enabled": None if enabled is None else bool(enabled),
                "items": [_item_state(item) for item in items],
            }
        )
    return tracks


def _item_state(item) -> dict:
    pool_item = _call(item, "GetMediaPoolItem")
    return {
        "name": _call(item, "GetName"),
        "unique_id": _call(item, "GetUniqueId"),
        "media_id": _call(pool_item, "GetMediaId") if pool_item else None,
        "media_path": _media_path(pool_item),
        "start": _int(_call(item, "GetStart")),
        "end": _int(_call(item, "GetEnd")),
        "left_offset": _int(_call(item, "GetLeftOffset")),
        "right_offset": _int(_call(item, "GetRightOffset")),
        "source_start": _int(_call(item, "GetSourceStartFrame")),
        "source_end": _int(_call(item, "GetSourceEndFrame")),
        "enabled": _call(item, "GetClipEnabled"),
        "audio": _audio_properties(item),
    }


def _media_path(pool_item):
    if not pool_item:
        return None
    properties = _call(pool_item, "GetClipProperty", "File Path")
    if isinstance(properties, dict):  # some builds answer a single key with the whole table
        properties = properties.get("File Path")
    return properties or None


def _audio_properties(item) -> dict:
    properties = _call(item, "GetProperty")
    if not isinstance(properties, dict):
        return {}
    return {str(key): value for key, value in properties.items() if _is_audio_key(str(key))}


def _is_audio_key(key: str) -> bool:
    key = key.lower()
    return "volume" in key or "pan" in key


# ---------------------------------------------------------------------------
# Helpers


def _call(obj, method: str, *args):
    """``obj.method(*args)``; None when the object lacks it or Resolve raises (older APIs)."""
    if obj is None:
        return None
    try:
        return getattr(obj, method)(*args)
    except Exception:
        return None


def _int(value):
    try:
        return None if value is None else int(value)
    except (TypeError, ValueError):
        return None


def _valid(fp) -> bool:
    return isinstance(fp, str) and len(fp) == _FINGERPRINT_LEN and all(c in "0123456789abcdef" for c in fp)


def _entry_fp(name: str):
    stem, extension = os.path.splitext(name)
    return stem if extension and extension != ".part" else None


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _prune(cache_dir: str, keep: int, protect: str) -> None:
    entries = []
    for name in os.listdir(cache_dir):
        path = os.path.join(cache_dir, name)
        if _entry_fp(name) is not None and os.path.isfile(path):
            try:
                entries.append((path == protect, os.path.getmtime(path), path))
            except OSError:
                pass
    entries.sort(reverse=True)  # the entry just stored first, then newest to oldest
    for _, _, path in entries[max(keep, 1) :]:
        _remove(path)
