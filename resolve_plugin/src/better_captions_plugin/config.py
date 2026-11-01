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
# {timeline name: {"<audio track index>": {"role": "person"|"group"|"none", "name": speaker name}}} (track_picker)
TRACK_ROLES = "track_roles"
# before track roles: {timeline name: {"<audio track index>": speaker name}}; read as person tracks, and a
# timeline's entry moves to TRACK_ROLES when its tracks are next picked
TRACK_SPEAKERS = "track_speakers"
TRACK_ROLE_NAMES = ("person", "group", "none")

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


def _track_entries(config: dict, key: str, timeline_name: str) -> dict[int, object] | None:
    """``config[key][timeline_name]`` as {track index >= 1: value}; None when this timeline has no entry."""
    remembered = config.get(key)
    entries = remembered.get(timeline_name) if isinstance(remembered, dict) else None
    if not isinstance(entries, dict):
        return None
    out = {}
    for key_, value in entries.items():
        try:
            index = int(key_)
        except (TypeError, ValueError):
            continue
        if index >= 1:
            out[index] = value
    return out


def _legacy_track_speakers(config: dict, timeline_name: str) -> dict[int, str]:
    named = _track_entries(config, TRACK_SPEAKERS, timeline_name) or {}
    return {i: name.strip() for i, name in named.items() if isinstance(name, str) and name.strip()}


def remembered_track_speakers(config: dict, timeline_name: str) -> dict[int, str]:
    """{audio track index: speaker name} given last time for this timeline; {} when there are none."""
    entries = _track_entries(config, TRACK_ROLES, timeline_name)
    if entries is None:
        return _legacy_track_speakers(config, timeline_name)
    out = {}
    for index, entry in entries.items():
        name = entry.get("name") if isinstance(entry, dict) else None
        if isinstance(name, str) and name.strip():
            out[index] = name.strip()
    return out


def remembered_track_roles(config: dict, timeline_name: str) -> dict[int, str]:
    """{audio track index: role} chosen last time for this timeline (a track named in the older
    ``track_speakers`` is one person); {} when there are none."""
    entries = _track_entries(config, TRACK_ROLES, timeline_name)
    if entries is None:
        return {index: "person" for index in _legacy_track_speakers(config, timeline_name)}
    out = {}
    for index, entry in entries.items():
        role = entry.get("role") if isinstance(entry, dict) else None
        if role in TRACK_ROLE_NAMES:
            out[index] = role
    return out


def remember_audio_tracks(config: dict, timeline_name: str, indices, speakers=None, roles=None) -> bool:
    """Save the audio track choice for this timeline (other timelines' choices are kept), and with
    ``speakers`` ({track index: name}) or ``roles`` ({track index: role}) who talks on the chosen tracks;
    that moves the timeline out of the older ``track_speakers``. See ``save_settings``."""
    chosen = sorted({int(i) for i in indices})
    existing = config.get(AUDIO_TRACKS)
    remembered = dict(existing) if isinstance(existing, dict) else {}
    remembered[timeline_name] = chosen
    updates = {AUDIO_TRACKS: remembered}
    if speakers is not None or roles is not None:
        names = {int(i): str(n or "").strip() for i, n in (speakers or {}).items()}
        kinds = {int(i): r for i, r in (roles or {}).items() if r in TRACK_ROLE_NAMES}
        entries = {}
        for index in chosen:
            entry = {}
            if index in kinds:
                entry["role"] = kinds[index]
            if names.get(index):
                entry["name"] = names[index]
            if entry:
                entries[str(index)] = entry
        existing = config.get(TRACK_ROLES)
        by_timeline = dict(existing) if isinstance(existing, dict) else {}
        if entries:
            by_timeline[timeline_name] = entries
        else:
            by_timeline.pop(timeline_name, None)
        if by_timeline or TRACK_ROLES in config:
            updates[TRACK_ROLES] = by_timeline
        legacy = config.get(TRACK_SPEAKERS)
        if isinstance(legacy, dict) and timeline_name in legacy:
            updates[TRACK_SPEAKERS] = {k: v for k, v in legacy.items() if k != timeline_name}
    return save_settings(config, updates)


def engine_command(config: dict, audio_path: str | None, out_dir: str, fixed_tracks=None, tracks=None) -> list[str]:
    """``config["engine"] + ["run", audio, "--out", out_dir, <flags from user settings>]``, plus a
    ``--fixed-track NAME=PATH`` per ``fixed_tracks`` entry ``(speaker name, audio of that track alone)``
    and a ``--track ROLE:NAME=PATH`` per ``tracks`` entry ``(role, name, audio of that track alone)``.
    With ``tracks`` the engine transcribes each of them on its own; ``audio_path`` is then None."""
    engine = config.get("engine")
    if not isinstance(engine, (list, tuple)) or not engine or not all(isinstance(x, str) for x in engine):
        raise ConfigError(
            'config.json has no usable "engine" command (expected a list like '
            '["<venv python>", "-m", "better_captions.cli"]). Re-run the installer.'
        )
    if not os.path.exists(engine[0]):
        raise ConfigError("the engine Python %s no longer exists. Re-run the installer." % engine[0])
    argv = [*engine, "run"]
    if audio_path is not None:
        argv.append(str(audio_path))
    argv += ["--out", str(out_dir)]
    for key, flag in ENGINE_FLAGS:
        value = config.get(key)
        if value is None or value == "":
            continue
        argv += [flag, str(value)]
    for name, path in fixed_tracks or ():
        argv += ["--fixed-track", "%s=%s" % (name, path)]
    for role, name, path in tracks or ():
        argv += ["--track", "%s:%s=%s" % (role, name, path)]
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
