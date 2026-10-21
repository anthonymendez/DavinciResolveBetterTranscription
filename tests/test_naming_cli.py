"""Speaker naming through the logged-in claude / agy CLIs. subprocess.run is faked: no real CLI runs."""

import json
import logging
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from better_captions import cli as cli_mod
from better_captions import naming, secrets
from better_captions.naming import agent_cli, llm
from better_captions.naming.agent_cli import ANTIGRAVITY, CLAUDE
from better_captions.schema import Segment, Speaker, Transcript
from tests.test_naming import DIALOGUE, LLM_REPLY, FakeAnthropic, FakeOpenAI, make_transcript, names


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL"):
        monkeypatch.delenv(var, raising=False)
    home = tmp_path / "data"
    home.mkdir()
    monkeypatch.setenv("BETTER_CAPTIONS_HOME", str(home))


def write_config(**fields):
    (secrets.config_path()).write_text(json.dumps({"engine": ["py"], **fields}), encoding="utf-8")


def make_program(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    path.chmod(0o755)
    return str(path)


CLAUDE_OUT = json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": LLM_REPLY})
AGY_OUT = json.dumps({"status": "SUCCESS", "response": "```json\n" + LLM_REPLY + "\n```"})


class FakeRun:
    """Stands in for subprocess.run: records each call, answers with a CompletedProcess or raises."""

    def __init__(self, stdout="", returncode=0, stderr="", error=None):
        self.calls = []
        self.stdout, self.returncode, self.stderr, self.error = stdout, returncode, stderr, error

    def __call__(self, argv, **kwargs):
        self.calls.append((list(argv), kwargs))
        # The cwd is a fresh empty directory that exists while the CLI runs.
        assert os.path.isdir(kwargs["cwd"]) and os.listdir(kwargs["cwd"]) == []
        if self.error:
            raise self.error
        return subprocess.CompletedProcess(argv, self.returncode, self.stdout, self.stderr)


@pytest.fixture
def clis(monkeypatch, tmp_path):
    """Both CLIs installed under tmp_path/bin and found via the path settings."""
    paths = {"claude": make_program(tmp_path / "bin" / "claude"), "antigravity": make_program(tmp_path / "bin" / "agy")}
    write_config(claude_cli_path=paths["claude"], antigravity_cli_path=paths["antigravity"])
    return paths


def fake_run(monkeypatch, **kwargs):
    run = FakeRun(**kwargs)
    monkeypatch.setattr(agent_cli.subprocess, "run", run)
    return run


# --- command lines and reply parsing -------------------------------------------------------------


def test_claude_cli_command_line_and_reply(clis, monkeypatch):
    run = fake_run(monkeypatch, stdout=CLAUDE_OUT)
    result = naming.name_speakers(DIALOGUE, provider="claude-cli")
    assert names(result) == {"SPEAKER_00": "Maria", "SPEAKER_01": "Jordan"}
    assert {s.name_source for s in result} == {"llm"} and result[0].name_confidence == 0.85

    ((argv, kwargs),) = run.calls
    assert argv[0] == clis["claude"] and argv[1] == "-p"
    assert argv[3:] == ["--output-format", "json", "--tools", "", "--model", "haiku"]
    assert "--dangerously-skip-permissions" not in argv
    prompt = argv[2]
    assert prompt.startswith(llm.SYSTEM_PROMPT) and "SPEAKER_00: Hey Jordan, how are you?" in prompt
    assert kwargs["stdin"] is subprocess.DEVNULL and kwargs["timeout"] == 120
    assert kwargs["capture_output"] is True and kwargs["text"] is True
    assert os.path.basename(kwargs["cwd"]).startswith("better-captions-naming-")
    assert not os.path.exists(kwargs["cwd"])  # removed afterwards
    assert kwargs["env"]["PATH"].split(os.pathsep)[0] == os.path.dirname(clis["claude"])


def test_antigravity_cli_command_line_and_reply(clis, monkeypatch):
    run = fake_run(monkeypatch, stdout=AGY_OUT)
    result = naming.name_speakers(DIALOGUE, provider="antigravity-cli")
    assert names(result) == {"SPEAKER_00": "Maria", "SPEAKER_01": "Jordan"}
    assert {s.name_source for s in result} == {"llm"}
    ((argv, kwargs),) = run.calls
    assert argv[0] == clis["antigravity"] and argv[1] == "-p" and argv[3:] == ["--output-format", "json"]
    assert "SPEAKER_01: Good, thanks Maria." in argv[2]
    assert kwargs["stdin"] is subprocess.DEVNULL and "--dangerously-skip-permissions" not in argv


def test_model_and_timeout_options(clis, monkeypatch):
    run = fake_run(monkeypatch, stdout=AGY_OUT)
    naming.name_speakers(
        DIALOGUE, provider="antigravity-cli", antigravity_cli_model="gemini-3-pro", naming_cli_timeout=30
    )
    argv, kwargs = run.calls[-1]
    assert argv[-2:] == ["--model", "gemini-3-pro"] and kwargs["timeout"] == 30

    run = fake_run(monkeypatch, stdout=CLAUDE_OUT)
    naming.name_speakers(DIALOGUE, provider="claude-cli", model="sonnet", timeout=5)
    argv, kwargs = run.calls[-1]
    assert argv[-2:] == ["--model", "sonnet"] and kwargs["timeout"] == 5

    # From config.json too.
    write_config(claude_cli_path=clis["claude"], claude_cli_model="opus", naming_cli_timeout=45)
    naming.name_speakers(DIALOGUE, provider="claude-cli")
    argv, kwargs = run.calls[-1]
    assert argv[-2:] == ["--model", "opus"] and kwargs["timeout"] == 45


@pytest.mark.parametrize(
    "cli, stdout",
    [
        (CLAUDE, json.dumps({"result": "hi", "is_error": False})),
        (CLAUDE, "warming up...\n" + json.dumps({"result": "hi"})),
        (CLAUDE, json.dumps([{"type": "system"}, {"type": "result", "result": "hi"}])),
        (ANTIGRAVITY, json.dumps({"status": "SUCCESS", "response": "hi"})),
    ],
)
def test_reply_text_formats(cli, stdout):
    assert agent_cli.reply_text(cli, stdout) == "hi"


@pytest.mark.parametrize(
    "cli, stdout",
    [
        (CLAUDE, "not json"),
        (CLAUDE, json.dumps({"is_error": True, "result": "Not logged in"})),
        (CLAUDE, json.dumps({"result": None})),
        (CLAUDE, "[1, 2]"),
        (ANTIGRAVITY, json.dumps({"status": "ERROR", "error": "quota"})),
        (ANTIGRAVITY, json.dumps({"response": "hi"})),
        (ANTIGRAVITY, json.dumps({"status": "SUCCESS"})),
        (ANTIGRAVITY, ""),
    ],
)
def test_reply_text_rejects_bad_output(cli, stdout):
    with pytest.raises(llm.NamingError):
        agent_cli.reply_text(cli, stdout)


# --- failures fall back to the heuristic ---------------------------------------------------------


@pytest.mark.parametrize(
    "provider, run_kwargs, message",
    [
        ("claude-cli", {"error": subprocess.TimeoutExpired(["claude"], 120)}, "timed out after 120 s"),
        ("claude-cli", {"returncode": 1, "stderr": "Invalid API key"}, "exited with code 1: Invalid API key"),
        ("claude-cli", {"stdout": "<html>"}, "not JSON"),
        ("claude-cli", {"error": FileNotFoundError("gone")}, "could not start"),
        ("antigravity-cli", {"stdout": json.dumps({"status": "FAILED", "error": "not signed in"})}, "not signed in"),
        (
            "antigravity-cli",
            {"stdout": json.dumps({"status": "SUCCESS", "response": "Maria and Jordan"})},
            "no JSON object",
        ),
        ("antigravity-cli", {"error": subprocess.TimeoutExpired(["agy"], 120)}, "agy timed out"),
    ],
)
def test_failures_fall_back_to_heuristic(clis, monkeypatch, caplog, provider, run_kwargs, message):
    run = fake_run(monkeypatch, **run_kwargs)
    with caplog.at_level(logging.WARNING):
        result = naming.name_speakers(DIALOGUE, provider=provider)
    assert run.calls
    assert names(result) == {"SPEAKER_00": "Maria", "SPEAKER_01": "Jordan"}
    assert {s.name_source for s in result} == {"heuristic"}
    assert (
        f"{provider} speaker naming failed" in caplog.text and "NamingError" in caplog.text and message in caplog.text
    )


def test_missing_cli_falls_back_to_heuristic(monkeypatch, tmp_path, caplog):
    run = fake_run(monkeypatch, stdout=CLAUDE_OUT)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    monkeypatch.setattr(agent_cli, "_search_dirs", lambda platform, environ, home: [])
    with caplog.at_level(logging.WARNING):
        result = naming.name_speakers(DIALOGUE, provider="claude-cli")
    assert not run.calls and {s.name_source for s in result} == {"heuristic"}
    assert "Claude Code CLI (claude) not found" in caplog.text


# --- discovery ------------------------------------------------------------------------------------


def test_discovery_order_option_config_path_then_install_dirs(tmp_path, caplog):
    home = tmp_path / "home"
    # The PATH lookup is the host's shutil.which, which on a Windows host only matches PATHEXT names.
    on_path = make_program(tmp_path / "pathdir" / ("claude.exe" if sys.platform == "win32" else "claude"))
    local_bin = make_program(home / ".local" / "bin" / "claude")
    claude_local = make_program(home / ".claude" / "local" / "claude")
    configured = make_program(tmp_path / "configured" / "claude")
    option = make_program(tmp_path / "option" / "claude")
    env = {"PATH": str(tmp_path / "pathdir")}

    def find(options=None, environ=env):
        return agent_cli.find_cli(CLAUDE, options or {}, platform="linux", environ=environ, home=str(home))

    write_config(claude_cli_path=configured)
    assert find({"claude_cli_path": option}) == option
    assert find() == configured
    write_config()
    # On Windows, shutil.which returns the PATHEXT casing (claude.EXE).
    assert os.path.normcase(find()) == os.path.normcase(on_path)
    assert find(environ={"PATH": "/usr/bin/nothing-here"}) == local_bin  # Resolve's minimal PATH
    os.remove(local_bin)
    assert find(environ={"PATH": ""}) == claude_local
    os.remove(claude_local)
    assert find(environ={"PATH": ""}) is None or find(environ={"PATH": ""}) in (
        "/opt/homebrew/bin/claude",
        "/usr/local/bin/claude",
    )

    # A configured path that isn't there is reported and skipped.
    with caplog.at_level(logging.WARNING):
        assert os.path.normcase(find({"claude_cli_path": str(tmp_path / "missing")})) == os.path.normcase(on_path)
    assert "is not a program" in caplog.text


def test_posix_install_dirs_include_homebrew_and_usr_local(tmp_path):
    dirs = agent_cli._search_dirs("darwin", {}, str(tmp_path))
    assert dirs == [
        str(tmp_path / ".local" / "bin"),
        str(tmp_path / ".claude" / "local"),
        "/opt/homebrew/bin",
        "/usr/local/bin",
    ]


def test_windows_discovery_prefers_exe_and_checks_appdata(tmp_path):
    home, local, roaming = tmp_path / "home", tmp_path / "Local", tmp_path / "Roaming"
    env = {"PATH": "", "LOCALAPPDATA": str(local), "APPDATA": str(roaming)}

    def find(cli):
        return agent_cli.find_cli(cli, {}, platform="win32", environ=env, home=str(home))

    npm_cmd = make_program(roaming / "npm" / "claude.cmd")
    assert find(CLAUDE) == npm_cmd
    native = make_program(home / ".local" / "bin" / "claude.exe")
    assert find(CLAUDE) == native
    agy = make_program(local / "Programs" / "Antigravity" / "bin" / "agy.cmd")
    assert find(ANTIGRAVITY) == agy
    agy_exe = make_program(local / "Programs" / "agy.exe")
    assert find(ANTIGRAVITY) == agy_exe


def test_found_clis_and_doctor_report(tmp_path, monkeypatch, capsys):
    claude = make_program(tmp_path / "bin" / "claude")
    write_config(claude_cli_path=claude)
    monkeypatch.setenv("PATH", "")
    monkeypatch.setattr(agent_cli, "_search_dirs", lambda platform, environ, home: [])
    assert agent_cli.found_clis() == {"claude": claude, "antigravity": None}

    from better_captions.engine import gpu, transcribe

    monkeypatch.setattr(gpu, "setup", lambda: {"dirs": [], "loaded": []})
    monkeypatch.setattr(gpu, "nvidia_gpus", lambda: [])
    monkeypatch.setattr(gpu, "cuda_info", lambda: {})
    monkeypatch.setattr(transcribe, "_cuda_available", lambda: False)
    monkeypatch.setattr(transcribe, "_backend_available", lambda name: False)
    assert cli_mod.main(["doctor"]) == 0
    assert json.loads(capsys.readouterr().out)["naming_clis"] == {"claude": claude, "antigravity": None}


# --- Windows .cmd launchers are never run through cmd.exe ---------------------------------------

NPM_SHIM = r"""@ECHO off
GOTO start
:find_dp0
SET dp0=%~dp0
EXIT /b
:start
SETLOCAL
CALL :find_dp0

IF EXIST "%dp0%\node.exe" (
  SET "_prog=%dp0%\node.exe"
) ELSE (
  SET "_prog=node"
  SET PATHEXT=%PATHEXT:;.JS;=;%
)

endLocal & goto #_undefined_# 2>NUL || title %COMSPEC% & "%_prog%"  "%dp0%\node_modules\@anthropic-ai\claude-code\cli.js" %*
"""  # noqa: E501 (a verbatim npm shim)

ELECTRON_SHIM = r"""@echo off
setlocal
set ELECTRON_RUN_AS_NODE=1
"%~dp0..\Antigravity.exe" "%~dp0..\resources\app\out\cli.js" %*
endlocal
"""


def test_npm_cmd_shim_runs_node_directly(tmp_path):
    shim = tmp_path / "npm" / "claude.cmd"
    make_program(shim)
    shim.write_text(NPM_SHIM, encoding="utf-8")
    script = make_program(tmp_path / "npm" / "node_modules" / "@anthropic-ai" / "claude-code" / "cli.js")
    prefix, env = agent_cli.unwrap_cmd_shim(str(shim), which=lambda name: "/usr/bin/node")
    assert prefix == ["/usr/bin/node", script] and env == {}
    node = make_program(tmp_path / "npm" / "node.exe")  # node next to the shim wins
    assert agent_cli.unwrap_cmd_shim(str(shim), which=lambda name: None)[0] == [node, script]


def test_electron_cmd_shim_runs_the_app_as_node(tmp_path):
    shim = tmp_path / "Antigravity" / "bin" / "agy.cmd"
    make_program(shim)
    shim.write_text(ELECTRON_SHIM, encoding="utf-8")
    app = make_program(tmp_path / "Antigravity" / "Antigravity.exe")
    script = make_program(tmp_path / "Antigravity" / "resources" / "app" / "out" / "cli.js")
    assert agent_cli.unwrap_cmd_shim(str(shim)) == ([app, script], {"ELECTRON_RUN_AS_NODE": "1"})


def test_unknown_cmd_shim_is_refused(tmp_path, monkeypatch):
    shim = tmp_path / "claude.cmd"
    shim.write_text("@echo off\nsomething.exe %*\n", encoding="utf-8")
    with pytest.raises(llm.NamingError, match="not a recognised launcher"):
        agent_cli.unwrap_cmd_shim(str(shim))
    # Through name_speakers on Windows: refused before anything runs, heuristic fallback.
    run = fake_run(monkeypatch, stdout=CLAUDE_OUT)
    monkeypatch.setattr(agent_cli, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(agent_cli, "find_cli", lambda cli, options: str(shim))
    result = naming.name_speakers(DIALOGUE, provider="claude-cli")
    assert not run.calls and {s.name_source for s in result} == {"heuristic"}


# --- the prompt fits the OS command-line limit ----------------------------------------------------

LONG = make_transcript(*[(f"SPEAKER_0{i % 2}", f"line {i} " + "words and more words " * 5) for i in range(3000)])


def _cmdline_len(argv):
    return len(subprocess.list2cmdline(argv))


def test_cli_token_budget_is_lower_than_the_api_default():
    argv = agent_cli.build_command(CLAUDE, ["claude"], LONG.speakers, LONG.segments, {}, platform="linux")
    assert agent_cli.DEFAULT_CLI_TOKEN_BUDGET < llm.DEFAULT_TOKEN_BUDGET
    assert len(argv[2]) <= len(llm.SYSTEM_PROMPT) + 100 + agent_cli.DEFAULT_CLI_TOKEN_BUDGET * llm.CHARS_PER_TOKEN
    # A bigger requested budget is capped too.
    big = agent_cli.build_command(
        CLAUDE, ["claude"], LONG.speakers, LONG.segments, {"token_budget": 50000}, platform="linux"
    )
    assert big == argv


@pytest.mark.parametrize(
    "program, limit",
    [("C:\\bin\\claude.exe", agent_cli.WINDOWS_ARGV_LIMIT), ("C:\\npm\\agy.cmd", agent_cli.WINDOWS_BATCH_ARGV_LIMIT)],
)
def test_windows_command_line_is_truncated_to_fit(program, limit):
    quoted = make_transcript(
        *[(f"SPEAKER_0{i % 2}", 'she said "hi" ' * 40) for i in range(400)]
    )  # quotes grow when escaped
    argv = agent_cli.build_command(ANTIGRAVITY, [program], quoted.speakers, quoted.segments, {}, platform="win32")
    assert _cmdline_len(argv) <= limit
    assert "[... transcript truncated ...]" in argv[2] and argv[2].startswith(llm.SYSTEM_PROMPT)


def test_explicit_limit_and_impossible_fit():
    argv = agent_cli.build_command(CLAUDE, ["claude"], LONG.speakers, LONG.segments, {}, platform="linux", limit=5000)
    assert max(len(a.encode()) for a in argv) <= 5000
    with pytest.raises(llm.NamingError, match="does not fit"):
        agent_cli.build_command(CLAUDE, ["claude"], LONG.speakers, LONG.segments, {}, platform="linux", limit=500)


# --- auto order: anthropic key > openai > claude-cli > antigravity-cli > heuristic ---------------


@pytest.fixture
def installed(monkeypatch):
    """Which CLIs "auto" sees, by key; replaces conftest's none-installed default."""
    present = {"claude": "/x/claude", "antigravity": "/x/agy"}
    monkeypatch.setattr(agent_cli, "available", lambda cli, options: present.get(cli.key))
    return present


@pytest.fixture
def sdks(monkeypatch):
    anthropic, openai = FakeAnthropic(), FakeOpenAI()
    monkeypatch.setattr(llm, "_anthropic_client", lambda api_key, base_url: anthropic)
    monkeypatch.setattr(llm, "_openai_client", lambda api_key, base_url: openai)
    return SimpleNamespace(anthropic=anthropic, openai=openai)


@pytest.fixture
def asked(monkeypatch):
    calls = []
    reply = llm.parse_reply(LLM_REPLY, ["SPEAKER_00", "SPEAKER_01"])
    monkeypatch.setattr(agent_cli, "ask_claude_cli", lambda *args: calls.append("claude-cli") or reply)
    monkeypatch.setattr(agent_cli, "ask_antigravity_cli", lambda *args: calls.append("antigravity-cli") or reply)
    return calls


def test_auto_order(installed, sdks, asked, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("OPENAI_API_KEY", "o")
    naming.name_speakers(DIALOGUE)
    assert sdks.anthropic.calls and not sdks.openai.calls and not asked

    monkeypatch.delenv("ANTHROPIC_API_KEY")
    naming.name_speakers(DIALOGUE)
    assert sdks.openai.calls and not asked

    monkeypatch.delenv("OPENAI_API_KEY")
    result = naming.name_speakers(DIALOGUE)
    assert asked == ["claude-cli"] and {s.name_source for s in result} == {"llm"}

    del installed["claude"]
    naming.name_speakers(DIALOGUE)
    assert asked == ["claude-cli", "antigravity-cli"]

    del installed["antigravity"]
    result = naming.name_speakers(DIALOGUE)
    assert asked == ["claude-cli", "antigravity-cli"] and {s.name_source for s in result} == {"heuristic"}


def test_auto_cli_failure_falls_back_to_heuristic(installed, monkeypatch, caplog):
    run = fake_run(monkeypatch, returncode=2, stderr="boom")
    monkeypatch.setattr(agent_cli, "find_cli", lambda cli, options: "/x/claude")
    with caplog.at_level(logging.WARNING):
        result = naming.name_speakers(DIALOGUE, provider="auto")
    assert run.calls and {s.name_source for s in result} == {"heuristic"}
    assert "claude-cli speaker naming failed" in caplog.text


def test_providers_list_and_cli_help():
    assert naming.PROVIDERS == ("none", "heuristic", "anthropic", "openai", "claude-cli", "antigravity-cli", "auto")
    run = cli_mod.build_parser()._subparsers._group_actions[0].choices["run"]
    help_text = next(a.help for a in run._actions if a.dest == "naming")
    assert "claude-cli" in help_text and "antigravity-cli" in help_text


def test_user_names_are_kept(clis, monkeypatch):
    fake_run(monkeypatch, stdout=CLAUDE_OUT)
    t = Transcript(
        language="en",
        duration=2.0,
        speakers=[Speaker("SPEAKER_00", name="Ann", name_confidence=1.0, name_source="user"), Speaker("SPEAKER_01")],
        segments=DIALOGUE.segments,
    )
    result = naming.name_speakers(t, provider="claude-cli")
    assert names(result) == {"SPEAKER_00": "Ann", "SPEAKER_01": "Jordan"} and result[0].name_source == "user"


def test_no_speakers_runs_nothing(clis, monkeypatch):
    run = fake_run(monkeypatch, stdout=CLAUDE_OUT)
    empty = Transcript(
        language="en", duration=0.0, speakers=[], segments=[Segment(speaker="SPEAKER_00", start=0, end=1, text="")]
    )
    assert naming.name_speakers(empty, provider="claude-cli") == []
    assert not run.calls
