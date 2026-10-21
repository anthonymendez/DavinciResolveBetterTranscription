import json
import os
import sys

import pytest

from better_captions_plugin import config as config_mod


def test_data_dir_per_platform_and_override(tmp_path):
    assert config_mod.data_dir({"BETTER_CAPTIONS_HOME": str(tmp_path)}, platform="linux") == str(tmp_path)
    join = os.path.join
    assert config_mod.data_dir({"LOCALAPPDATA": "/la"}, platform="win32", home="/h") == join("/la", "BetterCaptions")
    assert config_mod.data_dir({}, platform="win32", home="/h") == join("/h", "AppData", "Local", "BetterCaptions")
    assert config_mod.data_dir({}, platform="darwin", home="/h") == "/h/Library/Application Support/BetterCaptions"
    assert config_mod.data_dir({}, platform="linux", home="/h") == "/h/.local/share/BetterCaptions"
    assert config_mod.data_dir({"XDG_DATA_HOME": "/xdg"}, platform="linux", home="/h") == "/xdg/BetterCaptions"


def test_config_path_uses_env_override(tmp_path):
    assert config_mod.config_path({"BETTER_CAPTIONS_HOME": str(tmp_path)}) == str(tmp_path / "config.json")


def test_missing_config_explains_installer(tmp_path):
    with pytest.raises(config_mod.ConfigError, match="installer"):
        config_mod.load_config(environ={"BETTER_CAPTIONS_HOME": str(tmp_path)})


def test_load_config_tolerates_bom_and_rejects_garbage(tmp_path):
    path = tmp_path / "config.json"
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps({"engine": [sys.executable, "-m", "x"], "version": "1"}).encode())
    config = config_mod.load_config(str(path))
    assert config["version"] == "1" and config["_path"] == str(path)
    path.write_text("[1, 2]")
    with pytest.raises(config_mod.ConfigError):
        config_mod.load_config(str(path))
    path.write_text("{broken")
    with pytest.raises(config_mod.ConfigError):
        config_mod.load_config(str(path))


def test_engine_command_adds_flags_from_user_settings():
    config = {"engine": [sys.executable, "-m", "better_captions.cli"], "model": "small", "naming_provider": "heuristic",
              "num_speakers": 2, "language": "", "engine_args": ["--max-duration", "5"]}
    argv = config_mod.engine_command(config, "/tmp/a.wav", "/tmp/out")
    assert argv[:7] == [sys.executable, "-m", "better_captions.cli", "run", "/tmp/a.wav", "--out", "/tmp/out"]
    assert argv[7:] == ["--model", "small", "--num-speakers", "2", "--naming", "heuristic", "--max-duration", "5"]


def test_engine_command_requires_usable_engine(tmp_path):
    with pytest.raises(config_mod.ConfigError, match="engine"):
        config_mod.engine_command({}, "a", "b")
    with pytest.raises(config_mod.ConfigError, match="no longer exists"):
        config_mod.engine_command({"engine": [str(tmp_path / "missing-python"), "-m", "x"]}, "a", "b")


def test_engine_environment_strips_resolve_python_settings():
    env = config_mod.engine_environment({"PYTHONPATH": "/resolve/modules", "PYTHONHOME": "/x", "PATH": "/bin", "RESOLVE_SCRIPT_API": "/y"})
    assert "PYTHONPATH" not in env and "PYTHONHOME" not in env and "RESOLVE_SCRIPT_API" not in env
    assert env["PATH"] == "/bin" and env["PYTHONIOENCODING"] == "utf-8"
