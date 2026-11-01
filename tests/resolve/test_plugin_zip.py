"""The launcher script and the zipped package it loads."""

import importlib.util
import os
import shutil
import subprocess
import sys
import zipfile

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PLUGIN = os.path.join(REPO, "resolve_plugin")
UTILITY = os.path.join(PLUGIN, "Utility")
LAUNCHER = os.path.join(UTILITY, "Better Captions.py")
ZIP = os.path.join(UTILITY, "better_captions_plugin.zip")
BUILD = os.path.join(PLUGIN, "build_plugin_zip.py")


def _build_module():
    spec = importlib.util.spec_from_file_location("build_plugin_zip", BUILD)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_zip_is_up_to_date_with_source():
    build = _build_module()
    assert os.path.isfile(ZIP), "run: uv run python resolve_plugin/build_plugin_zip.py"
    assert build.is_up_to_date(), (
        "resolve_plugin/Utility/better_captions_plugin.zip is stale; "
        "run: uv run python resolve_plugin/build_plugin_zip.py"
    )


def test_zip_is_deterministic_and_contains_only_the_package(tmp_path):
    build = _build_module()
    assert build.build_bytes() == build.build_bytes()
    names = zipfile.ZipFile(ZIP).namelist()
    assert "better_captions_plugin/__init__.py" in names
    assert "better_captions_plugin/app.py" in names
    assert "better_captions_plugin/ui/tk_ui.py" in names
    assert all(n.startswith("better_captions_plugin/") and n.endswith(".py") for n in names)
    out = tmp_path / "x.zip"
    assert build.build(str(out)) is True
    assert build.build(str(out)) is False  # unchanged -> not rewritten


def test_utility_folder_lists_only_the_launcher_as_a_script():
    """Resolve shows every .py/.lua under Scripts/Utility in the menu: there must be exactly one."""
    entries = sorted(os.listdir(UTILITY))
    scripts = [e for e in entries if e.lower().endswith((".py", ".lua"))]
    assert scripts == ["Better Captions.py"]
    assert not any(os.path.isdir(os.path.join(UTILITY, e)) for e in entries)


def test_launcher_runs_the_plugin_from_the_zip(tmp_path):
    """Execute the launcher like Resolve does (a script file with `resolve` etc. as globals)."""
    install = tmp_path / "Utility"
    install.mkdir()
    for name in os.listdir(UTILITY):
        with open(os.path.join(UTILITY, name), "rb") as src, open(install / name, "wb") as dst:
            dst.write(src.read())
    driver = tmp_path / "driver.py"
    driver.write_text(
        "import sys, runpy\n"
        "sys.path.insert(0, %r)\n"
        "import better_captions_plugin\n"  # a stale copy already imported from src: the launcher must replace it
        "stale = sys.modules['better_captions_plugin']\n"
        "runpy.run_path(%r, init_globals={'resolve': None, 'fusion': None, 'bmd': None}, run_name='__main__')\n"
        "import better_captions_plugin as loaded\n"
        "assert loaded is not stale, 'stale module kept'\n"
        "assert 'better_captions_plugin.zip' in loaded.__file__, loaded.__file__\n"
        "print('LOADED-FROM-ZIP')\n" % (os.path.join(PLUGIN, "src"), str(install / "Better Captions.py"))
    )
    env = dict(os.environ, BETTER_CAPTIONS_HOME=str(tmp_path / "nowhere"))
    result = subprocess.run([sys.executable, str(driver)], capture_output=True, text=True, env=env, cwd=str(tmp_path))
    assert result.returncode == 0, result.stderr
    # Without a config.json the flow stops before any UI is needed and explains the installer.
    assert "not installed for this user" in result.stdout and "LOADED-FROM-ZIP" in result.stdout, (
        result.stdout + result.stderr
    )


def test_launcher_falls_back_to_src_when_zip_is_missing(tmp_path):
    utility = tmp_path / "resolve_plugin" / "Utility"
    utility.mkdir(parents=True)
    src = tmp_path / "resolve_plugin" / "src"
    try:
        os.symlink(os.path.join(PLUGIN, "src"), src, target_is_directory=True)
    except OSError:  # Windows without the symlink privilege: a plain copy exercises the same fallback
        shutil.copytree(os.path.join(PLUGIN, "src"), src)
    with open(LAUNCHER, "rb") as handle:
        (utility / "Better Captions.py").write_bytes(handle.read())
    driver = tmp_path / "driver.py"
    driver.write_text(
        "import runpy, sys\n"
        "runpy.run_path(%r, init_globals={'resolve': None}, run_name='__main__')\n"
        % str(utility / "Better Captions.py")
    )
    env = dict(os.environ, BETTER_CAPTIONS_HOME=str(tmp_path / "nowhere"))
    result = subprocess.run([sys.executable, str(driver)], capture_output=True, text=True, env=env, cwd=str(tmp_path))
    # No config and no display: the ConfigError is printed (tkinter may be unavailable) and nothing crashes.
    assert result.returncode == 0, result.stderr
    assert "not installed" in result.stdout + result.stderr


def test_plugin_is_stdlib_only_and_never_imports_the_engine():
    """The plugin runs on Resolve's Python: no third-party packages, never `better_captions`."""
    import re

    build = _build_module()
    stdlib = set(sys.stdlib_module_names)
    for rel, full in build.source_files():
        with open(full, encoding="utf-8") as handle:
            for line in handle:
                match = re.match(r"\s*(?:from|import)\s+([A-Za-z_][\w.]*)", line)
                if not match:
                    continue
                root = match.group(1).split(".")[0]
                assert root != "better_captions", "%s imports the engine: %s" % (rel, line.strip())
                assert root in stdlib or root == "better_captions_plugin", "%s: non-stdlib import %s" % (
                    rel,
                    line.strip(),
                )
