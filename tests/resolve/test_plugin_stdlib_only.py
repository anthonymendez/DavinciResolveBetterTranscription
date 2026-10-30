"""The plugin runs on whatever system Python Resolve finds: stdlib only, nothing removed from newer
Pythons, and source the launcher's oldest admitted Python (3.8) can still parse.

CI runs tests/resolve on a bare Python 3.10-3.14 too; these checks cover every module, including
the ones no test imports."""

import ast
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PACKAGE_DIR = os.path.join(ROOT, "resolve_plugin", "src", "better_captions_plugin")
LAUNCHER = os.path.join(ROOT, "resolve_plugin", "Utility", "Better Captions.py")

# Removed from the stdlib in Python 3.12 (PEP 632, asyncore & co.) and 3.13 (PEP 594).
REMOVED_MODULES = {
    "distutils",
    "imp",
    "asynchat",
    "asyncore",
    "smtpd",
    "cgi",
    "cgitb",
    "crypt",
    "imghdr",
    "mailcap",
    "msilib",
    "nis",
    "nntplib",
    "ossaudiodev",
    "pipes",
    "sndhdr",
    "spwd",
    "sunau",
    "telnetlib",
    "uu",
    "xdrlib",
    "lib2to3",
    "audioop",
    "chunk",
}


def _plugin_files() -> list[str]:
    files = []
    for dirpath, dirnames, filenames in os.walk(PACKAGE_DIR):
        dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
        files.extend(os.path.join(dirpath, name) for name in sorted(filenames) if name.endswith(".py"))
    return [*files, LAUNCHER]


PLUGIN_FILES = _plugin_files()


def _rel(path: str) -> str:
    return os.path.relpath(path, ROOT).replace(os.sep, "/")


def _source(path: str) -> str:
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _imported_roots(tree: ast.AST) -> list[tuple[str, int]]:
    """Top-level module of every absolute import anywhere in the module (also inside functions)."""
    roots = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.extend((alias.name.split(".")[0], node.lineno) for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.append((node.module.split(".")[0], node.lineno))
    return roots


def test_scan_covers_the_package_and_the_launcher():
    names = {_rel(path) for path in PLUGIN_FILES}
    assert "resolve_plugin/Utility/Better Captions.py" in names
    assert "resolve_plugin/src/better_captions_plugin/app.py" in names
    assert "resolve_plugin/src/better_captions_plugin/ui/__init__.py" in names


@pytest.mark.parametrize("path", PLUGIN_FILES, ids=_rel)
def test_imports_are_stdlib_only(path):
    stdlib = set(sys.stdlib_module_names)
    for root, lineno in _imported_roots(ast.parse(_source(path), path)):
        assert root not in REMOVED_MODULES, "%s:%d imports %s, removed in Python 3.12/3.13" % (_rel(path), lineno, root)
        assert root in stdlib or root == "better_captions_plugin", "%s:%d imports non-stdlib %s" % (
            _rel(path),
            lineno,
            root,
        )


@pytest.mark.parametrize("path", PLUGIN_FILES, ids=_rel)
def test_source_parses_with_the_python_38_grammar(path):
    """The launcher admits Python 3.8, so it and the package it loads must at least parse there."""
    ast.parse(_source(path), path, feature_version=(3, 8))
