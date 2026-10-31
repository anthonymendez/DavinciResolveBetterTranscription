"""The transcription model picker: catalog, dialogs, and its place in app.run (no window is ever shown:
UIManager runs against the fake, Tk only with a display)."""

import json
import os
import sys
from pathlib import Path

import pytest

from better_captions_plugin import app, model_picker
from better_captions_plugin import config as config_mod
from tests.resolve.fake_resolve import make_world
from tests.resolve.fake_ui import FakeBackend, accept_all
from tests.resolve.test_ui import FakeBmd, FakeFusion

FAKE_ENGINE = os.path.join(os.path.dirname(__file__), "fake_engine.py")
DETAIL_FIELDS = ("size", "accuracy", "speed", "memory", "languages", "best_for")


# --- catalog


def test_catalog_names_are_models_the_engine_accepts():
    faster_whisper_utils = pytest.importorskip("faster_whisper.utils")
    names = [info.name for info in model_picker.CATALOG]
    assert len(names) == len(set(names))
    assert set(names) <= set(faster_whisper_utils._MODELS)
    # the repos used to spot downloads are the ones faster-whisper fetches from
    assert {n: model_picker.MODEL_REPOS[n] for n in names} == {n: faster_whisper_utils._MODELS[n] for n in names}


def test_catalog_has_the_models_users_expect():
    names = {info.name for info in model_picker.CATALOG}
    assert {"tiny", "base", "small", "medium", "large-v3", "large-v3-turbo", "distil-large-v3"} <= names
    assert model_picker.DEFAULT_MODEL in names


def test_every_model_has_all_detail_fields():
    for info in model_picker.CATALOG:
        for field in DETAIL_FIELDS:
            assert str(getattr(info, field)).strip(), "%s has no %s" % (info.name, field)
        assert "CPU" in info.speed and "GPU" in info.speed
        assert "RAM" in info.memory and "VRAM" in info.memory
        assert model_picker.model_details(info).count("\n") >= len(DETAIL_FIELDS) - 1


def test_language_support_is_flagged():
    by_name = {info.name: info for info in model_picker.CATALOG}
    assert by_name["distil-large-v3"].languages == model_picker.ENGLISH
    assert by_name["small.en"].languages == model_picker.ENGLISH
    assert by_name["large-v3"].languages == model_picker.MULTILINGUAL


def test_downloaded_models_are_flagged(tmp_path):
    hub = tmp_path / "hf" / "hub"
    snapshot = hub / "models--Systran--faster-whisper-small" / "snapshots" / "abc123"
    snapshot.mkdir(parents=True)
    (snapshot / "model.bin").write_bytes(b"x")
    half = hub / "models--Systran--faster-whisper-base" / "snapshots" / "def456"
    half.mkdir(parents=True)  # a download that never finished: no model.bin
    ggml = tmp_path / "home" / "models" / "whispercpp"
    ggml.mkdir(parents=True)
    (ggml / "ggml-tiny.bin").write_bytes(b"x")
    environ = {"HF_HUB_CACHE": str(hub), "BETTER_CAPTIONS_HOME": str(tmp_path / "home")}
    flags = {info.name: info.downloaded for info in model_picker.catalog("", environ=environ)}
    assert flags["small"] and flags["tiny"]
    assert not any(flag for name, flag in flags.items() if name not in ("small", "tiny"))
    small = next(i for i in model_picker.catalog("", environ=environ) if i.name == "small")
    assert model_picker.DOWNLOADED_MARK in model_picker.model_label(small)
    base = next(i for i in model_picker.catalog("", environ=environ) if i.name == "base")
    assert model_picker.DOWNLOADED_MARK not in model_picker.model_label(base)


def test_huggingface_cache_follows_the_usual_environment_variables(tmp_path):
    assert model_picker.huggingface_cache({"HF_HUB_CACHE": "/a"}) == "/a"
    assert model_picker.huggingface_cache({"HF_HOME": "/b"}) == os.path.join("/b", "hub")
    xdg = model_picker.huggingface_cache({"XDG_CACHE_HOME": "/c"}, home="/h")
    assert xdg == os.path.join("/c", "huggingface", "hub")
    assert model_picker.huggingface_cache({}, home="/h") == os.path.join("/h", ".cache", "huggingface", "hub")


def test_a_model_set_by_hand_stays_selectable():
    models = model_picker.catalog("my-custom-model", environ={"BETTER_CAPTIONS_HOME": os.devnull})
    assert models[-1].name == "my-custom-model"
    assert models[model_picker.default_selection(models, "my-custom-model")].name == "my-custom-model"


# --- preselection


def test_current_model_is_config_model_else_the_engine_default():
    assert model_picker.current_model({"model": "small"}) == "small"
    assert model_picker.current_model({}) == model_picker.DEFAULT_MODEL
    assert model_picker.current_model({"model": ""}) == model_picker.DEFAULT_MODEL


def test_default_selection_preselects_the_current_model():
    models = model_picker.catalog("", environ={"BETTER_CAPTIONS_HOME": os.devnull})
    for info in models:
        assert models[model_picker.default_selection(models, info.name)].name == info.name
    assert models[model_picker.default_selection(models, "")].name == model_picker.DEFAULT_MODEL
    assert models[model_picker.default_selection(models, "nonsense")].name == model_picker.DEFAULT_MODEL


# --- dialogs


def _uimanager_pick(script, current="small"):
    models = model_picker.catalog(current, environ={"BETTER_CAPTIONS_HOME": os.devnull})
    bmd = FakeBmd(script)
    fusion = FakeFusion()
    dispatcher = bmd.UIDispatcher(fusion.UIManager)
    result = model_picker.pick_model_uimanager(fusion, fusion.UIManager, dispatcher, models, current)
    return result, dispatcher.windows[-1], models


def test_uimanager_picker_starts_on_the_current_model_and_returns_the_choice():
    seen = {}

    def script(window):
        items = window.GetItems()
        combo = items["ModelChoice"]
        seen["start"] = combo.items[combo.CurrentIndex]
        seen["details"] = items["ModelDetails"].Text
        combo.CurrentIndex = [m.name for m in seen["models"]].index("large-v3")
        window.On.ModelChoice.CurrentIndexChanged()
        seen["changed"] = items["ModelDetails"].Text
        window.On.Ok.Clicked()

    models = model_picker.catalog("small", environ={"BETTER_CAPTIONS_HOME": os.devnull})
    seen["models"] = models
    result, window, _ = _uimanager_pick(script)
    assert result == "large-v3"
    assert seen["start"].startswith("small  ")
    assert "Download size: 485 MB" in seen["details"]
    assert "Download size: 3.1 GB" in seen["changed"] and "Best for:" in seen["changed"]
    assert window.shown is False


def test_uimanager_picker_ok_returns_the_preselected_model():
    assert _uimanager_pick(lambda window: window.On.Ok.Clicked())[0] == "small"


def test_uimanager_picker_cancel_and_close_return_none():
    assert _uimanager_pick(lambda window: window.On.Cancel.Clicked())[0] is None
    assert _uimanager_pick(lambda window: window.On[model_picker.WINDOW_ID_MODEL].Close())[0] is None


@pytest.fixture
def tk_root():
    from tests.resolve.conftest import skip_unless_gui_tests

    skip_unless_gui_tests()
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


def test_tk_picker_round_trip(tk_root):
    models = model_picker.catalog("small", environ={"BETTER_CAPTIONS_HOME": os.devnull})
    dialog = model_picker._TkModelDialog(tk_root, models, "small")
    assert models[dialog.var.get()].name == "small"
    assert "485 MB" in dialog.details_var.get()
    dialog.var.set([m.name for m in models].index("medium"))
    assert "1.5 GB" in dialog.details_var.get()
    dialog._ok()
    assert dialog.result == "medium"


def test_tk_picker_cancel(tk_root):
    models = model_picker.catalog("small", environ={"BETTER_CAPTIONS_HOME": os.devnull})
    dialog = model_picker._TkModelDialog(tk_root, models, "small")
    dialog._cancel()
    assert dialog.result is None


# --- the flow


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("BETTER_CAPTIONS_HOME", str(tmp_path / "home"))
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"model": "small"}))
    return {
        "engine": [sys.executable, "-I", FAKE_ENGINE],
        "model": "small",
        "naming_provider": "heuristic",
        "_path": str(path),
    }


def _run(backend, config):
    return app.main(resolve=make_world(), backend=backend, config=config, sleep=lambda s: None)


def _engine_line(backend):
    lines = [line for view in backend.progress_views for line in view.lines if line.startswith("Engine: ")]
    assert lines, "the engine was never started"
    return lines[0].split()


def test_the_current_config_model_is_preselected_and_listed(config):
    backend = FakeBackend(accept_all(subtitles=True, textplus=False))
    outcome = _run(backend, config)
    assert outcome.error is None and not outcome.cancelled
    assert len(backend.model_requests) == 1
    models, current = backend.model_requests[0]
    assert current == "small"
    assert [m.name for m in models] == [m.name for m in model_picker.CATALOG]
    assert all(isinstance(m.downloaded, bool) for m in models)


def test_without_a_config_model_the_engine_default_is_preselected(config):
    del config["model"]
    backend = FakeBackend(accept_all(subtitles=True, textplus=False))
    outcome = _run(backend, config)
    assert outcome.error is None
    assert backend.model_requests[0][1] == model_picker.DEFAULT_MODEL


def test_the_choice_is_saved_to_config_and_passed_to_the_engine(config):
    backend = FakeBackend(accept_all(subtitles=True, textplus=False), model_answer="medium")
    outcome = _run(backend, config)
    assert outcome.error is None and not outcome.cancelled
    argv = _engine_line(backend)
    assert argv[argv.index("--model") + 1] == "medium"
    assert config["model"] == "medium"
    with open(config["_path"]) as handle:
        assert json.load(handle)["model"] == "medium"


def test_the_saved_choice_is_preselected_next_time(config):
    _run(FakeBackend(accept_all(subtitles=True, textplus=False), model_answer="large-v3"), config)
    saved = config_mod.load_config(config["_path"])
    backend = FakeBackend(accept_all(subtitles=True, textplus=False))
    _run(backend, {**config, "model": saved["model"]})
    assert backend.model_requests[0][1] == "large-v3"


def test_keeping_the_preselected_model_leaves_the_config_alone(config):
    before = Path(config["_path"]).read_text()
    assert app.choose_model(FakeBackend(), config, app.Outcome()) == "small"
    assert Path(config["_path"]).read_text() == before
    backend = FakeBackend(accept_all(subtitles=True, textplus=False))
    _run(backend, config)
    argv = _engine_line(backend)
    assert argv[argv.index("--model") + 1] == "small"


def test_cancelling_the_picker_cancels_the_run(config):
    before = Path(config["_path"]).read_text()
    backend = FakeBackend(accept_all(subtitles=True, textplus=False), model_answer=None)
    outcome = _run(backend, config)
    assert outcome.cancelled and outcome.error is None
    assert backend.progress_views == [] and backend.track_requests == []
    assert Path(config["_path"]).read_text() == before


def test_the_model_is_asked_before_the_audio_tracks(config):
    from tests.resolve.fake_resolve import FakeTrack

    order = []
    resolve = make_world()
    resolve.GetProjectManager().GetCurrentProject().GetCurrentTimeline().tracks["audio"].append(FakeTrack("Music"))

    class Ordered(FakeBackend):
        def pick_model(self, models, current):
            order.append("model")
            return super().pick_model(models, current)

        def pick_audio_tracks(self, tracks, preselected):
            order.append("tracks")
            return super().pick_audio_tracks(tracks, preselected)

    backend = Ordered(accept_all(subtitles=True, textplus=False))
    outcome = app.main(resolve=resolve, backend=backend, config=config, sleep=lambda s: None)
    assert outcome.error is None
    assert order == ["model", "tracks"]


def test_an_unwritable_config_still_uses_the_choice_for_this_run(config):
    config["_path"] = os.path.join(os.path.dirname(config["_path"]), "missing", "config.json")
    backend = FakeBackend(accept_all(subtitles=True, textplus=False), model_answer="base")
    outcome = _run(backend, config)
    assert outcome.error is None
    argv = _engine_line(backend)
    assert argv[argv.index("--model") + 1] == "base"
    assert any("model choice" in w for w in outcome.warnings)


def test_the_default_backend_keeps_the_current_model():
    from better_captions_plugin.ui import Backend

    assert Backend().pick_model(model_picker.CATALOG, "medium") == "medium"
