"""Locate and read ``<data>/config.json`` written by the installer (docs/ARCHITECTURE.md, "Install layout")."""

from __future__ import annotations

import json
import os
import sys

APP_DIR_NAME = "BetterCaptions"
CONFIG_FILE = "config.json"
EDITS_DIR = "edits"
RENDERS_DIR = "renders"

# Review-dialog choices remembered between runs (save_settings).
HIGHLIGHT_WORDS = "highlight_words"  # bool, off by default
HIGHLIGHT_COLOR = "highlight_color"  # "#RRGGBB"
NAMING_PROVIDER = "naming_provider"  # engine --naming for the next run (ui.NAMING_CHOICES)
FORCE_RERENDER = "force_rerender"  # bool: always render, never reuse a cached render
AUDIO_TRACKS = "audio_tracks"  # {timeline name: [audio track indices]} chosen for the last run

# config.json key -> engine CLI flag. Values are passed through as strings.
ENGINE_FLAGS = (
    ("model", "--model"),
    ("language", "--language"),
    ("device", "--device"),
    ("num_speakers", "--num-speakers"),
    ("min_speakers", "--min-speakers"),
    ("max_speakers", "--max-speakers"),
    ("diarization_backend", "--diarization-backend"),
    (NAMING_PROVIDER, "--naming"),
    ("max_chars", "--max-chars"),
    ("max_lines", "--max-lines"),
    ("max_duration", "--max-duration"),
)


class ConfigError(Exception):
    pass


def data_dir(environ=None, platform=None, home=None) -> str:
    """``$BETTER_CAPTIONS_HOME`` if set, else the per-OS data dir + ``BetterCaptions``."""
    env = os.environ if environ is None else environ
    platform = platform or sys.platform
    home = home or os.path.expanduser("~")
    override = env.get("BETTER_CAPTIONS_HOME")
    if override:
        return os.path.expanduser(override)
    if platform == "win32":
        base = env.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
    elif platform == "darwin":
        base = os.path.join(home, "Library", "Application Support")
    else:
        base = env.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share")
    return os.path.join(base, APP_DIR_NAME)


def config_path(environ=None) -> str:
    return os.path.join(data_dir(environ), CONFIG_FILE)


def edits_dir(config: dict) -> str | None:
    """``<data>/edits``: speaker fixes kept per rendered audio, so a re-run can re-apply them."""
    path = config.get("_path")
    return os.path.join(os.path.dirname(str(path)), EDITS_DIR) if path else None


def renders_dir(config: dict) -> str | None:
    """``<data>/renders``: the last few timeline audio renders, reused while the timeline is unchanged."""
    path = config.get("_path")
    return os.path.join(os.path.dirname(str(path)), RENDERS_DIR) if path else None


def load_config(path=None, environ=None) -> dict:
    path = path or config_path(environ)
    if not os.path.isfile(path):
        raise ConfigError(
            "Better Captions is not installed for this user: %s is missing.\n"
            "Run the installer (see README.md), then try again." % path
        )
    try:
        with open(path, encoding="utf-8-sig") as handle:
            config = json.load(handle)
    except ValueError as exc:
        raise ConfigError("%s is not valid JSON (%s). Re-run the installer." % (path, exc)) from exc
    if not isinstance(config, dict):
        raise ConfigError("%s does not contain a JSON object. Re-run the installer." % path)
    config.setdefault("_path", path)
    return config


def save_settings(config: dict, updates: dict) -> bool:
    """Remember review-dialog choices in config.json; returns whether the file changed.

    The file is re-read first and only ``updates`` are changed, so whatever the installer or
    the user wrote meanwhile is kept; it is replaced atomically. ``config`` is updated too.
    Raises OSError or ConfigError (an unreadable file is never overwritten).
    """
    config.update(updates)
    path = config.get("_path")
    if not path:
        return False
    with open(path, encoding="utf-8-sig") as handle:
        try:
            current = json.load(handle)
        except ValueError as exc:
            raise ConfigError("%s is not valid JSON (%s); settings not saved" % (path, exc)) from exc
    if not isinstance(current, dict):
        raise ConfigError("%s does not contain a JSON object; settings not saved" % path)
    if all(current.get(key) == value for key, value in updates.items()):
        return False
    current.update(updates)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(current, handle, indent=2, ensure_ascii=False)
        handle.write("\n")
    os.replace(tmp, path)
    return True


def remembered_audio_tracks(config: dict, timeline_name: str) -> list[int] | None:
    """The audio track indices picked last time for this timeline, or None when there are none."""
    remembered = config.get(AUDIO_TRACKS)
    chosen = remembered.get(timeline_name) if isinstance(remembered, dict) else None
    if not isinstance(chosen, (list, tuple)):
        return None
    indices = sorted({int(i) for i in chosen if isinstance(i, int) and not isinstance(i, bool) and i >= 1})
    return indices or None


def remember_audio_tracks(config: dict, timeline_name: str, indices) -> bool:
    """Save the audio track choice for this timeline (other timelines' choices are kept); see ``save_settings``."""
    existing = config.get(AUDIO_TRACKS)
    remembered = dict(existing) if isinstance(existing, dict) else {}
    remembered[timeline_name] = sorted({int(i) for i in indices})
    return save_settings(config, {AUDIO_TRACKS: remembered})


def engine_command(config: dict, audio_path: str, out_dir: str) -> list[str]:
    """``config["engine"] + ["run", audio, "--out", out_dir, <flags from user settings>]``."""
    engine = config.get("engine")
    if not isinstance(engine, (list, tuple)) or not engine or not all(isinstance(x, str) for x in engine):
        raise ConfigError(
            'config.json has no usable "engine" command (expected a list like '
            '["<venv python>", "-m", "better_captions.cli"]). Re-run the installer.'
        )
    if not os.path.exists(engine[0]):
        raise ConfigError("the engine Python %s no longer exists. Re-run the installer." % engine[0])
    argv = [*engine, "run", str(audio_path), "--out", str(out_dir)]
    for key, flag in ENGINE_FLAGS:
        value = config.get(key)
        if value is None or value == "":
            continue
        argv += [flag, str(value)]
    extra = config.get("engine_args")
    if isinstance(extra, (list, tuple)):
        argv += [str(x) for x in extra]
    return argv


def engine_environment(base=None) -> dict[str, str]:
    """Environment for the engine subprocess: Resolve's Python settings must not leak into the venv."""
    env = dict(os.environ if base is None else base)
    for key in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "RESOLVE_SCRIPT_API", "RESOLVE_SCRIPT_LIB"):
        env.pop(key, None)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    return env
