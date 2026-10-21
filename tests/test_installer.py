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
        BETTER_CAPTIONS_SKIP_SYSTEM_PYTHON="1",
    )
    return env


def _run(script: str, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["bash", str(INSTALL / script)], env=env, capture_output=True, text=True, timeout=600)
    assert result.returncode == 0, f"{script} failed:\n{result.stdout}\n{result.stderr}"
    return result


def _layout(env: dict[str, str]) -> tuple[Path, Path]:
    home = Path(env["HOME"])
    if sys.platform == "darwin":
        data = home / "Library" / "Application Support" / "BetterCaptions"
        utility = (
            home
            / "Library"
            / "Application Support"
            / "Blackmagic Design"
            / "DaVinci Resolve"
            / "Fusion"
            / "Scripts"
            / "Utility"
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
    assert config["gpu"] in ("cuda", "none")
    version = subprocess.run(
        [str(python), "-c", "import sys, better_captions; print(sys.version_info[:2])"],
        capture_output=True,
        text=True,
        check=True,
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
    assert f"Better Captions 0.1.0 is installed (from {source})" in first.stdout
    assert "Skipping the system Python check" in first.stdout
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


@pytest.mark.parametrize("gpu", ["cuda", "none"])
def test_gpu_override_recorded(sandbox: dict[str, str], source: Path, gpu: str) -> None:
    # With BETTER_CAPTIONS_SKIP_DEPS the `cuda` extra is requested ("<src>[cuda]") but not downloaded.
    sandbox["BETTER_CAPTIONS_GPU"] = gpu
    result = _run("install.sh", sandbox)
    config = _assert_installed(sandbox, source)
    on_macos = sys.platform == "darwin"
    assert config["gpu"] == ("none" if on_macos else gpu)
    if gpu == "cuda" and not on_macos:
        assert "also installing its CUDA runtime" in result.stdout
        assert "The NVIDIA GPU runtime (CUDA) is installed" in result.stdout
    else:
        assert "CUDA runtime was not installed" in result.stdout
    assert "better_captions.cli doctor" in result.stdout

    # `doctor` works in the installed engine, even without the model libraries.
    python = _layout(sandbox)[0] / "venv" / "bin" / "python"
    doctor = subprocess.run(
        [str(python), "-m", "better_captions.cli", "doctor"], capture_output=True, text=True, timeout=120
    )
    assert doctor.returncode == 0, doctor.stderr
    report = json.loads(doctor.stdout)
    assert report["backend"] == "faster-whisper" and "nvidia_gpus" in report


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


def _fake_python(path: Path, info: str | None) -> Path:
    """An executable that answers install.sh's probe with `info` ("3.12 64"), or fails like a Python without tkinter."""
    path.parent.mkdir(parents=True, exist_ok=True)
    body = f'echo "{info}"' if info else "echo 'No module named tkinter' >&2; exit 1"
    path.write_text(f"#!/bin/sh\n{body}\n")
    path.chmod(0o755)
    return path


def _ensure_resolve_python(
    os_name: str, tmp_path: Path, pythons: list[Path], **env: str
) -> subprocess.CompletedProcess[str]:
    """Runs only install.sh's ensure_resolve_python step, checking `pythons` instead of Resolve's locations."""
    workdir = tmp_path / "work"
    workdir.mkdir(exist_ok=True)
    run_env = {k: v for k, v in os.environ.items() if not k.startswith("BETTER_CAPTIONS_")}
    run_env.update(
        BETTER_CAPTIONS_LIB="1",
        BETTER_CAPTIONS_NONINTERACTIVE="1",
        # A non-empty list that matches nothing when no Pythons are given.
        BETTER_CAPTIONS_RESOLVE_PYTHONS=":".join(str(p) for p in pythons) or str(tmp_path / "none"),
    )
    run_env.update(env)
    return subprocess.run(
        [
            "bash",
            "-c",
            'source "$1"; ensure_resolve_python "$2" "$3"',
            "_",
            str(INSTALL / "install.sh"),
            os_name,
            str(workdir),
        ],
        env=run_env,
        capture_output=True,
        text=True,
        timeout=60,
        start_new_session=True,
    )


def test_resolve_python_finds_suitable_fake(tmp_path: Path) -> None:
    good = _fake_python(tmp_path / "dir with space" / "python3", "3.12 64")
    pythons = [tmp_path / "missing" / "python3", _fake_python(tmp_path / "old" / "python3", "3.9 64"), good]
    result = _ensure_resolve_python("linux", tmp_path, pythons)
    assert result.returncode == 0, result.stderr
    assert f"Resolve will use Python 3.12 at {good}" in result.stdout
    assert "sudo" not in result.stderr


@pytest.mark.parametrize("info", ["3.9 64", "3.13 64", "3.12 32", None])
def test_resolve_python_rejects_unsuitable(tmp_path: Path, info: str | None) -> None:
    bad = _fake_python(tmp_path / "bad" / "python3", info)
    result = _ensure_resolve_python("linux", tmp_path, [bad])
    assert result.returncode == 0, result.stderr
    assert "Resolve will use" not in result.stdout
    assert "Resolve needs a 64-bit Python 3.10-3.12 with tkinter" in result.stderr


def test_resolve_python_skip_flag(tmp_path: Path) -> None:
    good = _fake_python(tmp_path / "good" / "python3", "3.12 64")
    result = _ensure_resolve_python("linux", tmp_path, [good], BETTER_CAPTIONS_SKIP_SYSTEM_PYTHON="1")
    assert result.returncode == 0, result.stderr
    assert "Skipping the system Python check" in result.stdout
    assert "Resolve will use" not in result.stdout


def test_resolve_python_linux_prints_package_hint(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    _fake_python(bin_dir / "apt-get", "should not run")
    path = f"{bin_dir}:{os.environ['PATH']}"
    result = _ensure_resolve_python("linux", tmp_path, [], PATH=path)
    assert result.returncode == 0, result.stderr
    assert "sudo apt-get install python3 python3-tk" in result.stderr
    assert "doesn't install system packages" in result.stderr


def test_resolve_python_macos_noninteractive_skips_download(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    marker = tmp_path / "downloaded"
    for tool in ("curl", "sudo"):
        (bin_dir / tool).parent.mkdir(parents=True, exist_ok=True)
        (bin_dir / tool).write_text(f"#!/bin/sh\ntouch '{marker}'\nexit 1\n")
        (bin_dir / tool).chmod(0o755)
    path = f"{bin_dir}:{os.environ['PATH']}"
    result = _ensure_resolve_python("macos", tmp_path, [], PATH=path)
    assert result.returncode == 0, result.stderr
    assert "not running interactively" in result.stderr
    assert "python.org" in result.stderr
    assert not marker.exists()


def test_bad_source_fails_clearly(sandbox: dict[str, str], tmp_path: Path) -> None:
    sandbox["BETTER_CAPTIONS_SOURCE"] = str(tmp_path / "missing")
    result = subprocess.run(
        ["bash", str(INSTALL / "install.sh")], env=sandbox, capture_output=True, text=True, timeout=600
    )
    assert result.returncode != 0
    assert "has no pyproject.toml" in result.stderr
