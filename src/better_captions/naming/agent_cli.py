"""Speaker naming through the user's logged-in coding-agent CLIs: Claude Code and Antigravity.

No API key is needed: the CLI runs under the user's own account, so the transcript goes to
Anthropic (claude) or Google (agy) with that account's terms. The CLI is run non-interactively
with stdin closed, in a fresh empty directory, with tools/permissions left at their safe defaults.
The prompt is passed as an argument (agy ignores stdin), so it is kept under the OS's command-line
limits. Every failure raises NamingError so the caller falls back to the heuristic.

Settings come from the naming options, then ``<data>/config.json``: ``claude_cli_path``,
``antigravity_cli_path``, ``claude_cli_model``, ``antigravity_cli_model``, ``naming_cli_timeout``.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass

from better_captions import secrets
from better_captions.naming.llm import CHARS_PER_TOKEN, SYSTEM_PROMPT, LLMName, NamingError, build_prompt, parse_reply
from better_captions.schema import Segment, Speaker

log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 120.0
# Smaller than the API providers' budget: the whole prompt has to fit in one command-line argument.
DEFAULT_CLI_TOKEN_BUDGET = 6000
MIN_TOKEN_BUDGET = 200
# Command-line limits: CreateProcess takes 32767 chars, cmd.exe (.cmd/.bat) 8191; Linux caps a
# single argument at 128 KiB. Kept a little under each.
WINDOWS_ARGV_LIMIT = 32000
WINDOWS_BATCH_ARGV_LIMIT = 8000
POSIX_ARG_LIMIT = 120_000


@dataclass(frozen=True)
class AgentCLI:
    provider: str  # naming provider name
    key: str  # short name for settings and doctor
    executable: str  # base name of the binary
    label: str
    default_model: str | None


CLAUDE = AgentCLI("claude-cli", "claude", "claude", "Claude Code", "haiku")
ANTIGRAVITY = AgentCLI("antigravity-cli", "antigravity", "agy", "Antigravity", None)
CLIS = (CLAUDE, ANTIGRAVITY)


def _config() -> dict:
    return secrets._read_config(secrets.config_path())


def _setting(options: dict, key: str):
    """Naming option, else the engine's config.json, else None."""
    value = options.get(key)
    if value in (None, ""):
        value = _config().get(key)
    return None if value in (None, "") else value


def _search_dirs(platform: str, environ, home: str) -> list[str]:
    """Where the CLIs' installers put them; Resolve starts the engine with a minimal PATH."""
    dirs = [os.path.join(home, ".local", "bin"), os.path.join(home, ".claude", "local")]
    if platform == "win32":
        local, roaming = environ.get("LOCALAPPDATA"), environ.get("APPDATA")
        if local:
            programs = os.path.join(local, "Programs")
            dirs += [programs, os.path.join(programs, "Antigravity", "bin"), os.path.join(programs, "Antigravity")]
        if roaming:
            dirs.append(os.path.join(roaming, "npm"))
    else:
        dirs += ["/opt/homebrew/bin", "/usr/local/bin"]
    return dirs


def _is_program(path: str, platform: str) -> bool:
    if not os.path.isfile(path):
        return False
    return platform == "win32" or os.access(path, os.X_OK)


def find_cli(
    cli: AgentCLI, options: dict | None = None, platform: str | None = None, environ=None, home: str | None = None
) -> str | None:
    """The CLI's path: ``<key>_cli_path`` option/config, then PATH, then common install dirs; or None."""
    options = options or {}
    platform = platform or sys.platform
    environ = os.environ if environ is None else environ
    home = home or os.path.expanduser("~")

    explicit = _setting(options, f"{cli.key}_cli_path")
    if explicit:
        explicit = os.path.expanduser(str(explicit))
        if _is_program(explicit, platform):
            return explicit
        log.warning("%s_cli_path %s is not a program; looking for %s elsewhere", cli.key, explicit, cli.executable)

    found = shutil.which(cli.executable, path=environ.get("PATH", ""))
    if found:
        return found

    names = [cli.executable + ext for ext in (".exe", ".cmd")] if platform == "win32" else [cli.executable]
    for directory in _search_dirs(platform, environ, home):
        for name in names:
            candidate = os.path.join(directory, name)
            if _is_program(candidate, platform):
                return candidate
    return None


def available(cli: AgentCLI, options: dict) -> str | None:
    """find_cli with the run's options; the hook "auto" uses (tests replace it)."""
    return find_cli(cli, options)


def found_clis(options: dict | None = None) -> dict[str, str | None]:
    """{"claude": path or None, "antigravity": path or None}, for `better-captions doctor`."""
    return {cli.key: find_cli(cli, options or {}) for cli in CLIS}


# --- Windows .cmd launchers ---------------------------------------------------------------------

_SCRIPT_EXTS = (".js", ".mjs", ".cjs")


def unwrap_cmd_shim(path: str, which=shutil.which) -> tuple[list[str], dict[str, str]]:
    """(argv prefix, extra env) running a .cmd launcher's target directly.

    cmd.exe would re-parse the prompt (newlines end the command; & | % ^ are live), so the
    transcript must never go through it. npm shims run ``node <script.js> %*``; Electron-style
    launchers run ``<App.exe> <cli.js> %*`` with ELECTRON_RUN_AS_NODE=1.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError as exc:
        raise NamingError(f"cannot read {path}: {exc}") from exc
    base = os.path.dirname(os.path.abspath(path))

    def expand(token: str) -> str:
        for var in ("%~dp0", "%dp0%"):
            token = token.replace(var, base + os.sep)
        return os.path.normpath(token.replace("\\", os.sep))

    for line in text.splitlines():
        if "%*" not in line:
            continue
        quoted = re.findall(r'"([^"]+)"', line.split("%*")[0])
        scripts = [i for i, token in enumerate(quoted) if token.lower().endswith(_SCRIPT_EXTS)]
        if not scripts:
            continue
        index = scripts[-1]
        script = expand(quoted[index])
        runner = expand(quoted[index - 1]) if index else ""
        if "%" in script or not os.path.isfile(script):
            continue
        if (
            runner.lower().endswith(".exe")
            and "%" not in runner
            and os.path.basename(runner).lower() != "node.exe"
            and os.path.isfile(runner)
        ):
            return [runner, script], {"ELECTRON_RUN_AS_NODE": "1"}
        node = os.path.join(base, "node.exe")
        if not os.path.isfile(node):
            node = which("node")
        if not node:
            raise NamingError(f"{path} needs node, which was not found")
        return [node, script], {}
    raise NamingError(f"cannot run {path} safely: not a recognised launcher; set the CLI path to its .exe")


def _program(binary: str, platform: str) -> tuple[list[str], dict[str, str]]:
    if platform == "win32" and binary.lower().endswith((".cmd", ".bat")):
        return unwrap_cmd_shim(binary)
    return [binary], {}


# --- prompt and command line ---------------------------------------------------------------------


def _argv_too_long(argv: list[str], platform: str, limit: int | None) -> bool:
    if platform == "win32":
        batch = argv[0].lower().endswith((".cmd", ".bat"))
        return len(subprocess.list2cmdline(argv)) > (
            limit or (WINDOWS_BATCH_ARGV_LIMIT if batch else WINDOWS_ARGV_LIMIT)
        )
    return max(len(a.encode("utf-8")) for a in argv) > (limit or POSIX_ARG_LIMIT)


def build_command(
    cli: AgentCLI,
    prefix: list[str],
    speakers: list[Speaker],
    segments: list[Segment],
    options: dict,
    platform: str | None = None,
    limit: int | None = None,
) -> list[str]:
    """The full argv, the transcript cut down until the command line fits the OS limit."""
    platform = platform or sys.platform
    model = (
        options.get(f"{cli.key}_cli_model")
        or options.get("model")
        or _setting({}, f"{cli.key}_cli_model")
        or cli.default_model
    )
    budget = min(int(options.get("token_budget") or DEFAULT_CLI_TOKEN_BUDGET), DEFAULT_CLI_TOKEN_BUDGET)
    while True:
        prompt = f"{SYSTEM_PROMPT}\n\n{build_prompt(speakers, segments, budget)}"
        argv = [*prefix, "-p", prompt, "--output-format", "json"]
        if cli is CLAUDE:
            argv += ["--tools", ""]
        if model:
            argv += ["--model", str(model)]
        if not _argv_too_long(argv, platform, limit):
            return argv
        if budget <= MIN_TOKEN_BUDGET:
            raise NamingError("the prompt does not fit on the command line")
        budget = max(MIN_TOKEN_BUDGET, budget * 3 // 4)


# --- running and parsing -------------------------------------------------------------------------


def _load_json(stdout: str):
    text = (stdout or "").strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    for line in reversed(text.splitlines()):  # tolerate chatter before the JSON line
        line = line.strip()
        if line.startswith("{"):
            try:
                return json.loads(line)
            except ValueError:
                break
    raise NamingError(f"CLI output is not JSON: {text[:200]!r}")


def reply_text(cli: AgentCLI, stdout: str) -> str:
    """The model's reply from the CLI's ``--output-format json`` output."""
    data = _load_json(stdout)
    if cli is CLAUDE:
        if isinstance(data, list):  # an event list: the last "result" event holds the reply
            data = next((e for e in reversed(data) if isinstance(e, dict) and e.get("type") == "result"), None)
        if not isinstance(data, dict):
            raise NamingError("claude output is not a JSON object")
        if data.get("is_error"):
            raise NamingError(f"claude reported an error: {str(data.get('result') or data.get('subtype'))[:200]}")
        result = data.get("result")
    else:
        if not isinstance(data, dict):
            raise NamingError("agy output is not a JSON object")
        if data.get("status") != "SUCCESS":
            detail = data.get("error") or data.get("message") or data.get("response") or ""
            raise NamingError(f"agy status {data.get('status')!r}: {str(detail)[:200]}")
        result = data.get("response")
    if not isinstance(result, str):
        raise NamingError(f"{cli.executable} output has no reply text")
    return result


def run_cli(
    cli: AgentCLI, argv: list[str], extra_env: dict[str, str], timeout: float, platform: str | None = None
) -> str:
    """Run the CLI and return its stdout; NamingError on start failure, timeout or nonzero exit."""
    platform = platform or sys.platform
    env = dict(os.environ)
    env.update(extra_env)
    # `#!/usr/bin/env node` launchers need their own dir (where node usually is) on the minimal PATH.
    env["PATH"] = os.pathsep.join(p for p in (os.path.dirname(argv[0]), env.get("PATH", "")) if p)
    kwargs = {}
    if platform == "win32":
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    with tempfile.TemporaryDirectory(prefix="better-captions-naming-") as cwd:
        try:
            proc = subprocess.run(
                argv,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
                cwd=cwd,
                env=env,
                **kwargs,
            )
        except subprocess.TimeoutExpired as exc:
            raise NamingError(f"{cli.executable} timed out after {timeout:g} s") from exc
        except OSError as exc:
            raise NamingError(f"could not start {argv[0]}: {exc}") from exc
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip()[-300:]
        raise NamingError(f"{cli.executable} exited with code {proc.returncode}: {tail}")
    return proc.stdout


def ask(cli: AgentCLI, speakers: list[Speaker], segments: list[Segment], options: dict) -> dict[str, LLMName | None]:
    binary = find_cli(cli, options)
    if not binary:
        raise NamingError(f"{cli.label} CLI ({cli.executable}) not found; set {cli.key}_cli_path in config.json")
    prefix, extra_env = _program(binary, sys.platform)
    argv = build_command(cli, prefix, speakers, segments, options)
    timeout = float(
        options.get("naming_cli_timeout")
        or options.get("timeout")
        or _setting({}, "naming_cli_timeout")
        or DEFAULT_TIMEOUT
    )
    stdout = run_cli(cli, argv, extra_env, timeout)
    return parse_reply(reply_text(cli, stdout), [s.id for s in speakers])


def ask_claude_cli(speakers: list[Speaker], segments: list[Segment], options: dict) -> dict[str, LLMName | None]:
    return ask(CLAUDE, speakers, segments, options)


def ask_antigravity_cli(speakers: list[Speaker], segments: list[Segment], options: dict) -> dict[str, LLMName | None]:
    return ask(ANTIGRAVITY, speakers, segments, options)
