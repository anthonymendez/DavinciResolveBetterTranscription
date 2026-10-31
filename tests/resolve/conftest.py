"""A test run never opens windows: tkinter tests are opt-in (BETTER_CAPTIONS_GUI_TESTS=1), and the
X11/Wayland display is hidden from the run and its subprocesses (WSLg would show them on the Windows
desktop)."""

import os

import pytest

GUI_TESTS = os.environ.get("BETTER_CAPTIONS_GUI_TESTS") == "1"

if not GUI_TESTS:
    for _name in ("DISPLAY", "WAYLAND_DISPLAY"):
        os.environ.pop(_name, None)


def skip_unless_gui_tests():
    if not GUI_TESTS:
        pytest.skip("tkinter tests open real windows; set BETTER_CAPTIONS_GUI_TESTS=1 to run them")
