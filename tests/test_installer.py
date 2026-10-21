"""Installer tests: syntax checks plus a sandboxed install/upgrade/uninstall run.

The end-to-end tests run install.sh against a fake HOME in tmp_path with
BETTER_CAPTIONS_SKIP_DEPS=1, so only the package itself is installed. uv's
cache and Python install dir are pointed at the real ones so the uv-managed
Python 3.12 is downloaded at most once per machine.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
INSTALL = REPO / "install"
SH_SCRIPTS = ["install.sh", "uninstall.sh"]
PS_SCRIPTS = ["install.ps1", "uninstall.ps1"]

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="bash installers")


def _uv_dir(*args: str) -> str:
    return subprocess.run(["uv", *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture(scope="module")
def uv_dirs() -> dict[str, str]:
    if shutil.which("uv") is None:
        pytest.skip("uv not installed")
    return {
        "UV_CACHE_DIR": _uv_dir("cache", "dir"),
        "UV_PYTHON_INSTALL_DIR": _uv_dir("python", "dir"),
    }


@pytest.fixture
def source(tmp_path: Path) -> Path:
    """The repo root, or a copy with a stub plugin if the plugin isn't there yet."""
    if (REPO / "resolve_plugin" / "Utility").is_dir():
        return REPO
    src = tmp_path / "source"
    src.mkdir()
    for name in ("pyproject.toml", "README.md"):
        shutil.copy2(REPO / name, src / name)
    shutil.copytree(REPO / "src", src / "src", ignore=shutil.ignore_patterns("__pycache__"))
    utility = src / "resolve_plugin" / "Utility"
    (utility / "BetterCaptions").mkdir(parents=True)
    (utility / "Better Captions.py").write_text("# stub plugin\n")
    (utility / "BetterCaptions" / "ui.py").write_text("# stub module\n")
    return src


@pytest.fixture
def sandbox(tmp_path: Path, uv_dirs: dict[str, str], source: Path) -> dict[str, str]:
    home = tmp_path / "home"
    tmp = tmp_path / "tmp"
    home.mkdir()
    tmp.mkdir()
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("BETTER_CAPTIONS_", "UV_", "XDG_")) and k not in ("VIRTUAL_ENV", "PYTHONPATH")
    }
    env.update(uv_dirs)
    env.update(
        HOME=str(home),
        XDG_DATA_HOME=str(home / ".local" / "share"),
        XDG_CONFIG_HOME=str(home / ".config"),
        XDG_CACHE_HOME=str(home / ".cache"),
        TMPDIR=str(tmp),
        BETTER_CAPTIONS_SOURCE=str(source),
        BETTER_CAPTIONS_SKIP_DEPS="1",
    )
    return env


def _run(script: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["bash", str(INSTALL / script)], env=env, capture_output=True, text=True, timeout=600
    )
    assert result.returncode == 0, f"{script} failed:\n{result.stdout}\n{result.stderr}"
    return result


def _layout(env: dict[str, str]) -> tuple[Path, Path]:
    home = Path(env["HOME"])
    if sys.platform == "darwin":
        data = home / "Library" / "Application Support" / "BetterCaptions"
        utility = (
            home / "Library" / "Application Support" / "Blackmagic Design" / "DaVinci Resolve"
            / "Fusion" / "Scripts" / "Utility"
        )
    else:
        data = Path(env["XDG_DATA_HOME"]) / "BetterCaptions"
        utility = home / ".local" / "share" / "DaVinciResolve" / "Fusion" / "Scripts" / "Utility"
    return Path(env.get("BETTER_CAPTIONS_HOME", data)), utility


def _plugin_entries(source: Path) -> list[str]:
    return sorted(p.name for p in (source / "resolve_plugin" / "Utility").iterdir())


def _assert_installed(env: dict[str, str], source: Path) -> dict:
    data, utility = _layout(env)
    python = data / "venv" / "bin" / "python"
    assert python.is_file()
    config = json.loads((data / "config.json").read_text())
    assert config["engine"] == [str(python), "-m", "better_captions.cli"]
    assert config["version"] == "0.1.0"
    version = subprocess.run(
        [str(python), "-c", "import sys, better_captions; print(sys.version_info[:2])"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    assert version == "(3, 12)"
    for name in _plugin_entries(source):
        assert (utility / name).exists(), name
    return config


@pytest.mark.parametrize("script", SH_SCRIPTS)
def test_sh_syntax(script: str) -> None:
    subprocess.run(["bash", "-n", str(INSTALL / script)], check=True)


@pytest.mark.parametrize("script", PS_SCRIPTS)
def test_ps1_parses(script: str) -> None:
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("pwsh not installed")
    check = (
        "$errs = $null; "
        f"[System.Management.Automation.Language.Parser]::ParseFile('{INSTALL / script}', [ref]$null, [ref]$errs) | Out-Null; "
        "if ($errs) { $errs | ForEach-Object { Write-Error $_.ToString() }; exit 1 }"
    )
    subprocess.run([pwsh, "-NoProfile", "-Command", check], check=True)


def test_install_upgrade_uninstall(sandbox: dict[str, str], source: Path) -> None:
    data, utility = _layout(sandbox)

    first = _run("install.sh", sandbox)
    _assert_installed(sandbox, source)
    assert "Workspace > Scripts" in first.stdout
    assert "GPU" in first.stdout
    if not Path("/opt/resolve").exists() and not Path("/Applications/DaVinci Resolve").exists():
        assert "Resolve was not found" in first.stderr

    # A user setting and a file next to the plugin must survive an upgrade.
    config_path = data / "config.json"
    config = json.loads(config_path.read_text())
    config["naming_provider"] = "anthropic"
    config_path.write_text(json.dumps(config))
    (utility / "someone-elses-script.lua").write_text("-- not ours\n")

    second = _run("install.sh", sandbox)
    assert "Reusing engine venv" in second.stdout
    assert _assert_installed(sandbox, source)["naming_provider"] == "anthropic"

    _run("uninstall.sh", sandbox)
    assert not data.exists()
    for name in _plugin_entries(source):
        assert not (utility / name).exists(), name
    assert (utility / "someone-elses-script.lua").exists()

    # Uninstalling twice is harmless.
    _run("uninstall.sh", sandbox)


def test_home_override(sandbox: dict[str, str], source: Path, tmp_path: Path) -> None:
    custom = tmp_path / "custom home"
    sandbox["BETTER_CAPTIONS_HOME"] = str(custom)
    _run("install.sh", sandbox)
    _assert_installed(sandbox, source)
    default_data, _ = _layout({k: v for k, v in sandbox.items() if k != "BETTER_CAPTIONS_HOME"})
    assert not default_data.exists()

    (custom / "keep.txt").write_text("user file")
    _run("uninstall.sh", sandbox)
    assert not (custom / "venv").exists()
    assert not (custom / "config.json").exists()
    assert (custom / "keep.txt").exists()


def test_bad_source_fails_clearly(sandbox: dict[str, str], tmp_path: Path) -> None:
    sandbox["BETTER_CAPTIONS_SOURCE"] = str(tmp_path / "missing")
    result = subprocess.run(
        ["bash", str(INSTALL / "install.sh")], env=sandbox, capture_output=True, text=True, timeout=600
    )
    assert result.returncode != 0
    assert "has no pyproject.toml" in result.stderr
