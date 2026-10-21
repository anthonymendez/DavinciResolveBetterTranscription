"""Release tests: one package version everywhere, the release workflow's shape,
and the installers' choice of git ref (with a faked GitHub API, no network).

How releases are cut: docs/RELEASING.md.
"""

import importlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

import better_captions
import better_captions_plugin

REPO = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO / ".github" / "workflows"
CHECK_VERSION = REPO / ".github" / "scripts" / "check_release_version.py"
INSTALL_SH = REPO / "install" / "install.sh"
INSTALL_PS1 = REPO / "install" / "install.ps1"

RELEASE_JSON = {
    "url": "https://api.github.com/repos/anthonymendez/DavinciResolveBetterTranscription/releases/123",
    "tag_name": "v1.2.3",
    "name": "Better Captions 1.2.3",
    "draft": False,
    "prerelease": False,
}
RATE_LIMITED_JSON = {
    "message": "API rate limit exceeded for 203.0.113.7. (But here's the good news: ...)",
    "documentation_url": "https://docs.github.com/rest/overview/resources-in-the-rest-api#rate-limiting",
}
NOT_FOUND_JSON = {"message": "Not Found", "documentation_url": "https://docs.github.com/rest/releases/releases"}

bash_only = pytest.mark.skipif(sys.platform == "win32", reason="bash installer")


def _project_version() -> str:
    with open(REPO / "pyproject.toml", "rb") as f:
        return tomllib.load(f)["project"]["version"]


def _workflow(name: str) -> dict:
    data = yaml.safe_load((WORKFLOWS / name).read_text(encoding="utf-8"))
    # YAML 1.1 reads the bare key `on` as True.
    data["on"] = data.pop(True, data.get("on"))
    return data


# --- version ---------------------------------------------------------------


def test_versions_match_pyproject() -> None:
    version = _project_version()
    assert importlib.metadata.version("better-captions") == version
    assert better_captions.__version__ == version
    # The plugin runs on Resolve's Python without package metadata, so it keeps a copy.
    assert better_captions_plugin.__version__ == version


def test_version_reads_package_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(importlib.metadata, "version", lambda name: "9.8.7")
    try:
        assert importlib.reload(better_captions).__version__ == "9.8.7"
    finally:
        monkeypatch.undo()
        importlib.reload(better_captions)


def test_version_fallback_when_not_installed(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(name: str) -> str:
        raise importlib.metadata.PackageNotFoundError(name)

    monkeypatch.setattr(importlib.metadata, "version", missing)
    try:
        assert importlib.reload(better_captions).__version__ == "0+unknown"
    finally:
        monkeypatch.undo()
        importlib.reload(better_captions)
    assert better_captions.__version__ == _project_version()


def _check_version(tag: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, str(CHECK_VERSION), tag], capture_output=True, text=True, timeout=60)


def test_release_tag_check_accepts_matching_tag() -> None:
    result = _check_version(f"v{_project_version()}")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == _project_version()


@pytest.mark.parametrize("tag", ["v99.0.0", _project_version(), "v1.2", "release-1"])
def test_release_tag_check_rejects_mismatch(tag: str) -> None:
    result = _check_version(tag)
    assert result.returncode == 1
    assert "error:" in result.stderr
    assert result.stdout == ""


# --- workflows -------------------------------------------------------------


def test_release_workflow_triggers_on_version_tags_only() -> None:
    on = _workflow("release.yml")["on"]
    assert set(on) == {"push"}
    assert on["push"] == {"tags": ["v*.*.*"]}


def test_ci_is_reusable_and_skips_tag_pushes() -> None:
    on = _workflow("ci.yml")["on"]
    assert "workflow_call" in on
    assert "pull_request" in on
    # Branch-only push trigger, so a release tag doesn't run CI twice.
    assert on["push"] == {"branches": ["**"]}
    assert "tags" not in on["push"]


def test_release_workflow_runs_ci_before_publishing() -> None:
    jobs = _workflow("release.yml")["jobs"]
    callers = [name for name, job in jobs.items() if job.get("uses") == "./.github/workflows/ci.yml"]
    assert len(callers) == 1
    release = jobs["release"]
    needs = release["needs"]
    assert callers[0] in ([needs] if isinstance(needs, str) else needs)
    assert release["permissions"]["contents"] == "write"


def _release_steps() -> list[dict]:
    return _workflow("release.yml")["jobs"]["release"]["steps"]


def _step_index(steps: list[dict], text: str) -> int:
    matches = [i for i, step in enumerate(steps) if text in step.get("run", "")]
    assert matches, f"no step runs {text!r}"
    return matches[0]


def test_release_workflow_steps() -> None:
    steps = _release_steps()
    checkout = next(s for s in steps if s.get("uses", "").startswith("actions/checkout@"))
    assert checkout["with"]["fetch-depth"] == 0  # history and tags for the notes

    check = _step_index(steps, "check_release_version.py")
    assert '"$GITHUB_REF_NAME"' in steps[check]["run"]
    build = _step_index(steps, "uv build")
    archive = _step_index(steps, "git archive --format=zip")
    notes = _step_index(steps, "git log")
    publish = _step_index(steps, "gh release create")
    assert check < build < archive < publish
    assert notes < publish

    assert "install/install.sh" in steps[archive]["run"] and "install/install.ps1" in steps[archive]["run"]
    assert "git describe --tags" in steps[notes]["run"]
    run = steps[publish]["run"]
    for asset in ("dist/*.whl", "dist/*.tar.gz", "dist/*-source.zip", "dist/install.sh", "dist/install.ps1"):
        assert asset in run, asset
    assert "--notes-file release-notes.md" in run
    assert "--verify-tag" in run
    assert steps[publish]["env"]["GH_TOKEN"] == "${{ github.token }}"


# --- installer ref resolution (install.sh) -----------------------------------


def _api_file(tmp_path: Path, response: dict | str) -> Path:
    path = tmp_path / "releases-latest.json"
    path.write_text(response if isinstance(response, str) else json.dumps(response, indent=2))
    return path


def _sh(tmp_path: Path, command: str, **env: str) -> subprocess.CompletedProcess[str]:
    """Runs `command` in bash with install.sh's functions loaded and no network: curl is a stub that fails."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    curl_log = tmp_path / "curl.log"
    (bin_dir / "curl").write_text(f'#!/bin/sh\necho "$@" >> "{curl_log}"\nexit 22\n')
    (bin_dir / "curl").chmod(0o755)
    run_env = {k: v for k, v in os.environ.items() if not k.startswith("BETTER_CAPTIONS_")}
    run_env.update(BETTER_CAPTIONS_LIB="1", PATH=f"{bin_dir}:{os.environ['PATH']}")
    run_env.update(env)
    return subprocess.run(
        ["bash", "-c", f'source "$1"; {command}', "_", str(INSTALL_SH)],
        env=run_env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _curl_calls(tmp_path: Path) -> str:
    log = tmp_path / "curl.log"
    return log.read_text() if log.exists() else ""


@bash_only
def test_sh_defaults_to_latest_release(tmp_path: Path) -> None:
    api = _api_file(tmp_path, RELEASE_JSON)
    result = _sh(tmp_path, "install_ref", BETTER_CAPTIONS_RELEASE_API=str(api))
    assert result.returncode == 0, result.stderr
    assert result.stdout == "v1.2.3\n"
    assert "Latest release: v1.2.3" in result.stderr
    assert "warning" not in result.stderr


@bash_only
def test_sh_reads_minified_api_response(tmp_path: Path) -> None:
    api = _api_file(tmp_path, json.dumps(RELEASE_JSON, separators=(",", ":")))
    result = _sh(tmp_path, "install_ref", BETTER_CAPTIONS_RELEASE_API=str(api))
    assert result.stdout == "v1.2.3\n", result.stderr


@bash_only
@pytest.mark.parametrize(
    "response",
    [RATE_LIMITED_JSON, NOT_FOUND_JSON, "<html>502 Bad Gateway</html>", None],
    ids=["rate-limited", "no-release", "garbage", "api-unreachable"],
)
def test_sh_falls_back_to_main_with_warning(tmp_path: Path, response: dict | str | None) -> None:
    api = _api_file(tmp_path, response) if response is not None else tmp_path / "missing.json"
    result = _sh(tmp_path, "install_ref", BETTER_CAPTIONS_RELEASE_API=str(api))
    assert result.returncode == 0, result.stderr
    assert result.stdout == "main\n"
    assert "warning: could not find the latest release" in result.stderr
    assert "rate-limited" in result.stderr


@bash_only
def test_sh_api_failure_over_http_falls_back(tmp_path: Path) -> None:
    # The stub curl fails like `curl -f` on a 403 rate-limit or 404 response.
    result = _sh(tmp_path, "install_ref")
    assert result.stdout == "main\n", result.stderr
    assert "warning: could not find the latest release" in result.stderr
    assert "api.github.com/repos/anthonymendez/DavinciResolveBetterTranscription/releases/latest" in _curl_calls(
        tmp_path
    )


@bash_only
@pytest.mark.parametrize("ref", ["main", "v0.1.0", "0ba793c", "0ba793c0f1e2d3c4b5a69788796a5b4c3d2e1f00"])
def test_sh_explicit_ref_wins_without_api_call(tmp_path: Path, ref: str) -> None:
    api = _api_file(tmp_path, RELEASE_JSON)
    result = _sh(tmp_path, "install_ref", BETTER_CAPTIONS_REF=ref, BETTER_CAPTIONS_RELEASE_API=str(api))
    assert result.returncode == 0, result.stderr
    assert result.stdout == f"{ref}\n"
    assert result.stderr == ""
    assert _curl_calls(tmp_path) == ""


@bash_only
def test_sh_downloads_the_resolved_ref(tmp_path: Path) -> None:
    api = _api_file(tmp_path, RELEASE_JSON)
    work = tmp_path / "work"
    work.mkdir()
    result = _sh(tmp_path, f'fetch_source "{work}" "$(install_ref)"', BETTER_CAPTIONS_RELEASE_API=str(api))
    # The stub curl fails the download, after recording which archive was asked for.
    assert result.returncode != 0
    assert "Downloading anthonymendez/DavinciResolveBetterTranscription@v1.2.3" in result.stderr
    assert "github.com/anthonymendez/DavinciResolveBetterTranscription/archive/v1.2.3.tar.gz" in _curl_calls(tmp_path)


# --- installer ref resolution (install.ps1) ----------------------------------


def _pwsh(tmp_path: Path, command: str, **env: str) -> subprocess.CompletedProcess[str]:
    pwsh = shutil.which("pwsh")
    if pwsh is None:
        pytest.skip("pwsh not installed")
    run_env = {k: v for k, v in os.environ.items() if not k.startswith("BETTER_CAPTIONS_")}
    run_env.update(BETTER_CAPTIONS_LIB="1")
    run_env.update(env)
    script = f"$ErrorActionPreference = 'Stop'; . '{INSTALL_PS1}'; {command}"
    return subprocess.run(
        [pwsh, "-NoProfile", "-NonInteractive", "-Command", script],
        env=run_env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_ps1_defaults_to_latest_release(tmp_path: Path) -> None:
    api = _api_file(tmp_path, RELEASE_JSON)
    result = _pwsh(tmp_path, 'Write-Output "ref=$(Get-InstallRef)"', BETTER_CAPTIONS_RELEASE_API=str(api))
    assert result.returncode == 0, result.stderr
    assert "ref=v1.2.3" in result.stdout
    assert "Latest release: v1.2.3" in result.stdout


@pytest.mark.parametrize(
    "response", [RATE_LIMITED_JSON, NOT_FOUND_JSON, None], ids=["rate-limited", "no-release", "api-unreachable"]
)
def test_ps1_falls_back_to_main_with_warning(tmp_path: Path, response: dict | None) -> None:
    api = _api_file(tmp_path, response) if response is not None else tmp_path / "missing.json"
    result = _pwsh(tmp_path, 'Write-Output "ref=$(Get-InstallRef 3>&1)"', BETTER_CAPTIONS_RELEASE_API=str(api))
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip().endswith("main")
    assert "could not find the latest release" in result.stdout


@pytest.mark.parametrize("ref", ["main", "v0.1.0", "0ba793c"])
def test_ps1_explicit_ref_wins(tmp_path: Path, ref: str) -> None:
    api = _api_file(tmp_path, RELEASE_JSON)
    result = _pwsh(
        tmp_path, 'Write-Output "ref=$(Get-InstallRef)"', BETTER_CAPTIONS_REF=ref, BETTER_CAPTIONS_RELEASE_API=str(api)
    )
    assert result.returncode == 0, result.stderr
    assert f"ref={ref}" in result.stdout
    assert "Latest release" not in result.stdout
