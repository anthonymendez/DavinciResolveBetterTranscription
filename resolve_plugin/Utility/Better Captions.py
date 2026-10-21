# Better Captions for DaVinci Resolve (Workspace > Scripts > Better Captions).
#
# This file is the only script Resolve lists; the plugin code lives next to it in
# better_captions_plugin.zip (a plain zip of resolve_plugin/src, loaded with the standard
# library's zipimport so Resolve's Scripts menu does not list every module as a script).
# Running from a repo checkout works too: ../src is used when the zip is missing.
#
# Resolve provides the globals `resolve`, `fusion` and `bmd` when it runs this script.

import os
import sys


def _script_dir():
    for candidate in (globals().get("__file__"), sys.argv[0] if sys.argv else None):
        if candidate and os.path.isfile(candidate):
            return os.path.dirname(os.path.abspath(candidate))
    # Resolve's script host does not always set __file__: fall back to the known Utility folders.
    home = os.path.expanduser("~")
    guesses = [
        os.path.join(
            os.environ.get("APPDATA", ""),
            "Blackmagic Design",
            "DaVinci Resolve",
            "Support",
            "Fusion",
            "Scripts",
            "Utility",
        ),
        os.path.join(
            home,
            "Library",
            "Application Support",
            "Blackmagic Design",
            "DaVinci Resolve",
            "Fusion",
            "Scripts",
            "Utility",
        ),
        os.path.join(home, ".local", "share", "DaVinciResolve", "Fusion", "Scripts", "Utility"),
    ]
    for guess in guesses:
        if os.path.isfile(os.path.join(guess, "better_captions_plugin.zip")):
            return guess
    return os.getcwd()


def _load_plugin():
    here = _script_dir()
    zip_path = os.path.join(here, "better_captions_plugin.zip")
    src_path = os.path.normpath(os.path.join(here, os.pardir, "src"))
    for path in (zip_path, src_path):
        if os.path.exists(path):
            if path in sys.path:
                sys.path.remove(path)
            sys.path.insert(0, path)
            break
    else:
        raise ImportError("better_captions_plugin.zip is missing next to %s; re-run the installer" % __name__)
    # Drop stale modules so an upgrade takes effect without restarting Resolve's script host.
    for name in list(sys.modules):
        if name == "better_captions_plugin" or name.startswith("better_captions_plugin."):
            del sys.modules[name]
    try:
        import zipimport

        zipimport._zip_directory_cache.clear()  # type: ignore[attr-defined]
    except Exception:
        pass
    import better_captions_plugin.app as app

    return app


def _main():
    if sys.version_info < (3, 8):
        msg = (
            "Better Captions needs Python 3.8 or newer, but Resolve is running scripts with Python %s. "
            "Install a 64-bit Python 3.10-3.12 from python.org for all users and restart Resolve."
            % ".".join(map(str, sys.version_info[:3]))
        )
        print(msg)
        raise SystemExit(msg)
    app = _load_plugin()
    app.main(resolve=globals().get("resolve"), fusion=globals().get("fusion"), bmd=globals().get("bmd"))


if __name__ != "better_captions_launcher_test":
    _main()
