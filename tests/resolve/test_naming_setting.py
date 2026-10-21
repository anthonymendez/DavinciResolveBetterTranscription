"""The speaker-naming provider: picked in the review dialog (both backends), kept in config.json,
passed to the next engine run as --naming."""

import json
import os
import sys

import pytest

from better_captions_plugin import app
from better_captions_plugin import config as config_mod
from better_captions_plugin.timing import TimelineClock
from better_captions_plugin.transcript import from_dict
from better_captions_plugin.ui import NAMING_CHOICES, ReviewRequest, ReviewResult, SpeakerRow, naming_index
from better_captions_plugin.ui.uimanager_ui import UIManagerBackend

from tests.resolve.fake_engine import TRANSCRIPT
from tests.resolve.fake_resolve import make_world
from tests.resolve.fake_ui import FakeBackend, accept_all
from tests.resolve.test_ui import FakeBmd, FakeFusion

FAKE_ENGINE = os.path.join(os.path.dirname(__file__), "fake_engine.py")
VALUES = [value for value, _label in NAMING_CHOICES]


def test_choices_cover_every_engine_provider():
    from better_captions import naming

    assert sorted(VALUES) == sorted(naming.PROVIDERS)
    assert VALUES[0] == "auto"
    assert naming_index("claude-cli") == VALUES.index("claude-cli")
    assert naming_index("llm") == naming_index(None) == 0  # unknown values show as auto


def test_review_request_takes_the_configured_provider():
    clock = TimelineClock(fps=24.0, start_frame=0)
    transcript = from_dict(TRANSCRIPT)

    def request(config):
        return app.build_review_request(transcript, [], "TL", clock, 0, None, config)

    assert request({}).naming_provider == "auto"
    assert request({"naming_provider": "antigravity-cli"}).naming_provider == "antigravity-cli"
    assert request({"naming_provider": "bogus"}).naming_provider == "auto"


def test_provider_is_saved_in_config_json(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"engine": ["py"], "naming_provider": "heuristic", "model": "small"}), encoding="utf-8")
    config = config_mod.load_config(str(path))
    outcome = app.Outcome()
    app.remember_review_choices(config, ReviewResult(naming_provider="claude-cli"), outcome)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["naming_provider"] == "claude-cli" and saved["model"] == "small" and not outcome.warnings
    # Not chosen (None): config.json's value is kept.
    app.remember_review_choices(config, ReviewResult(), outcome)
    assert json.loads(path.read_text(encoding="utf-8"))["naming_provider"] == "claude-cli"


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"engine": [sys.executable, "-I", FAKE_ENGINE], "naming_provider": "heuristic"}), encoding="utf-8"
    )
    return config_mod.load_config(str(path))


def test_full_flow_choice_reaches_the_next_engine_run(config):
    backend = FakeBackend(accept_all(naming_provider="antigravity-cli"))
    outcome = app.main(resolve=make_world(), backend=backend, config=config, sleep=lambda s: None)
    assert outcome.error is None, backend.errors
    assert backend.requests[0].naming_provider == "heuristic"
    reloaded = config_mod.load_config(config["_path"])
    assert reloaded["naming_provider"] == "antigravity-cli"
    argv = config_mod.engine_command(reloaded, "a.wav", "out")
    assert argv[argv.index("--naming") + 1] == "antigravity-cli"
    second = FakeBackend(None)
    app.main(resolve=make_world(), backend=second, config=reloaded, sleep=lambda s: None)
    assert second.requests[0].naming_provider == "antigravity-cli"


def _request(**kwargs):
    return ReviewRequest(
        speakers=[SpeakerRow("SPEAKER_00", "Maria", 2, "Hey")], templates=["Style A  (Master)"], **kwargs
    )


def test_uimanager_review_has_the_naming_choice():
    def script(window):
        items = window.GetItems()
        assert items["Naming"].items == [label for _value, label in NAMING_CHOICES]
        assert items["Naming"].CurrentIndex == VALUES.index("openai")
        items["Naming"].CurrentIndex = VALUES.index("claude-cli")
        window.On.Ok.Clicked()

    result = UIManagerBackend(FakeFusion(), FakeBmd(script)).review(_request(naming_provider="openai"))
    assert result.naming_provider == "claude-cli"


def test_uimanager_naming_choice_survives_fix_speakers():
    from tests.resolve.test_ui import _editable_request

    seen = {"reviews": 0}

    def script(window):
        items = window.GetItems()
        if window.props["ID"] == "BetterCaptionsSpeakers":
            window.On.Done.Clicked()
            return
        seen["reviews"] += 1
        if seen["reviews"] == 1:
            items["Naming"].CurrentIndex = VALUES.index("antigravity-cli")
            window.On.FixSpeakers.Clicked()
        else:
            assert items["Naming"].CurrentIndex == VALUES.index("antigravity-cli")
            window.On.Ok.Clicked()

    result = UIManagerBackend(FakeFusion(), FakeBmd(script)).review(_editable_request())
    assert seen["reviews"] == 2 and result.naming_provider == "antigravity-cli"


@pytest.fixture
def tk_root():
    tk = pytest.importorskip("tkinter")
    try:
        root = tk.Tk()
    except tk.TclError as exc:
        pytest.skip("no display for tkinter: %s" % exc)
    root.withdraw()
    yield root
    try:
        root.destroy()
    except tk.TclError:
        pass


def test_tk_review_has_the_naming_choice(tk_root):
    from better_captions_plugin.ui import tk_ui

    dialog = tk_ui._ReviewDialog(tk_root, _request(naming_provider="heuristic"))
    assert list(dialog.naming_box["values"]) == [label for _value, label in NAMING_CHOICES]
    assert dialog.naming_box.current() == VALUES.index("heuristic")
    dialog.naming_box.current(VALUES.index("antigravity-cli"))
    dialog._ok()
    assert dialog.result.naming_provider == "antigravity-cli"
