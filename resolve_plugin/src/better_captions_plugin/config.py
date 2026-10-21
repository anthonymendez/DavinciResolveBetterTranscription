"""Locate and read ``<data>/config.json`` written by the installer (docs/ARCHITECTURE.md, "Install layout")."""

from __future__ import annotations

import json
import os
import sys

APP_DIR_NAME = "BetterCaptions"
CONFIG_FILE = "config.json"

# config.json key -> engine CLI flag. Values are passed through as strings.
ENGINE_FLAGS = (
    ("model", "--model"),
    ("language", "--language"),
    ("device", "--device"),
    ("num_speakers", "--num-speakers"),
    ("min_speakers", "--min-speakers"),
    ("max_speakers", "--max-speakers"),
    ("diarization_backend", "--diarization-backend"),
    ("naming_provider", "--naming"),
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


def load_config(path=None, environ=None) -> dict:
    path = path or config_path(environ)
    if not os.path.isfile(path):
        raise ConfigError(
            "Better Captions is not installed for this user: %s is missing.\n"
            "Run the installer (see README.md), then try again." % path
        )
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            config = json.load(handle)
    except ValueError as exc:
        raise ConfigError("%s is not valid JSON (%s). Re-run the installer." % (path, exc))
    if not isinstance(config, dict):
        raise ConfigError("%s does not contain a JSON object. Re-run the installer." % path)
    config.setdefault("_path", path)
    return config


def engine_command(config: dict, audio_path: str, out_dir: str) -> "list[str]":
    """``config["engine"] + ["run", audio, "--out", out_dir, <flags from user settings>]``."""
    engine = config.get("engine")
    if not isinstance(engine, (list, tuple)) or not engine or not all(isinstance(x, str) for x in engine):
        raise ConfigError(
            'config.json has no usable "engine" command (expected a list like '
            '["<venv python>", "-m", "better_captions.cli"]). Re-run the installer.'
        )
    if not os.path.exists(engine[0]):
        raise ConfigError("the engine Python %s no longer exists. Re-run the installer." % engine[0])
    argv = list(engine) + ["run", str(audio_path), "--out", str(out_dir)]
    for key, flag in ENGINE_FLAGS:
        value = config.get(key)
        if value is None or value == "":
            continue
        argv += [flag, str(value)]
    extra = config.get("engine_args")
    if isinstance(extra, (list, tuple)):
        argv += [str(x) for x in extra]
    return argv


def engine_environment(base=None) -> "dict[str, str]":
    """Environment for the engine subprocess: Resolve's Python settings must not leak into the venv."""
    env = dict(os.environ if base is None else base)
    for key in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "RESOLVE_SCRIPT_API", "RESOLVE_SCRIPT_LIB"):
        env.pop(key, None)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    return env
