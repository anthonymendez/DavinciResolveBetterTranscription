"""Choose the transcription model before a run.

``catalog()`` lists the models the engine supports (faster-whisper names, which the engine's `--model`
takes; see src/better_captions/engine/transcribe.py) with what a user needs to choose: download size,
accuracy, relative speed on CPU and GPU, memory needed, languages and a one-line "best for". Models
already on disk are flagged. The two dialogs (tkinter, Fusion UIManager) follow track_picker.py and
return the chosen model name, or None when cancelled; ``Backend.pick_model`` is what ``app`` calls.
tkinter is imported only when the Tk dialog is built, so UIManager-only installs never need it.
"""

from __future__ import annotations

import glob
import os
from dataclasses import dataclass, replace

from better_captions_plugin import config as config_mod

TITLE = "Better Captions"
WINDOW_ID_MODEL = "BetterCaptionsModel"
HEADING = "Transcription model"
NOTE = "Bigger models are more accurate but slower. A model that is not downloaded yet is fetched on first use."

DEFAULT_MODEL = "large-v3-turbo"  # the engine's --model default
ENGLISH = "English only"
MULTILINGUAL = "Multilingual (about 99 languages)"
DOWNLOADED_MARK = "downloaded"


@dataclass(frozen=True)
class ModelInfo:
    name: str  # the engine's --model value
    size: str  # download size
    accuracy: str
    speed: str  # relative speed on CPU and on a GPU
    memory: str  # RAM (CPU) / VRAM (GPU) needed
    languages: str
    best_for: str
    note: str = ""
    downloaded: bool = False


# Hugging Face repos faster-whisper downloads each name from (faster_whisper.utils._MODELS).
MODEL_REPOS = {
    "tiny": "Systran/faster-whisper-tiny",
    "base": "Systran/faster-whisper-base",
    "small": "Systran/faster-whisper-small",
    "small.en": "Systran/faster-whisper-small.en",
    "medium": "Systran/faster-whisper-medium",
    "medium.en": "Systran/faster-whisper-medium.en",
    "large-v3": "Systran/faster-whisper-large-v3",
    "large-v3-turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
    "distil-large-v3": "Systran/faster-distil-whisper-large-v3",
}

# Smallest to largest. Sizes are the faster-whisper downloads; speeds are relative to real time on
# typical hardware (rough guide: a mid-range GPU is several times faster than a modern CPU).
CATALOG = (
    ModelInfo(
        "tiny", "75 MB", "Basic", "CPU: very fast · GPU: instant", "~1 GB RAM · ~1 GB VRAM", MULTILINGUAL,
        "Quick drafts and checking the setup works",
    ),
    ModelInfo(
        "base", "145 MB", "Fair", "CPU: very fast · GPU: instant", "~1 GB RAM · ~1 GB VRAM", MULTILINGUAL,
        "Clean, close-mic speech when speed matters most",
    ),
    ModelInfo(
        "small", "485 MB", "Good", "CPU: fast · GPU: very fast", "~2 GB RAM · ~2 GB VRAM", MULTILINGUAL,
        "Everyday captions on a computer without a GPU",
    ),
    ModelInfo(
        "small.en", "485 MB", "Good (English)", "CPU: fast · GPU: very fast", "~2 GB RAM · ~2 GB VRAM", ENGLISH,
        "English-only footage on a computer without a GPU",
    ),
    ModelInfo(
        "medium", "1.5 GB", "Very good", "CPU: slow · GPU: fast", "~5 GB RAM · ~5 GB VRAM", MULTILINGUAL,
        "Noisier audio or accents when large models are too heavy",
    ),
    ModelInfo(
        "medium.en", "1.5 GB", "Very good (English)", "CPU: slow · GPU: fast", "~5 GB RAM · ~5 GB VRAM", ENGLISH,
        "English-only footage with background noise",
    ),
    ModelInfo(
        "large-v3-turbo", "1.6 GB", "Excellent (near large-v3)", "CPU: medium · GPU: very fast",
        "~6 GB RAM · ~4 GB VRAM", MULTILINGUAL,
        "The default: large-v3 quality at a fraction of the time",
    ),
    ModelInfo(
        "distil-large-v3", "1.5 GB", "Excellent (English)", "CPU: medium · GPU: very fast",
        "~6 GB RAM · ~4 GB VRAM", ENGLISH,
        "Fast, accurate English with a GPU",
        note="Not available on the Apple Silicon (MLX) and Vulkan GPU backends.",
    ),
    ModelInfo(
        "large-v3", "3.1 GB", "Best", "CPU: very slow · GPU: fast", "~10 GB RAM · ~8 GB VRAM", MULTILINGUAL,
        "The most accurate transcript, any language, when time is no object",
    ),
)  # fmt: skip


def _is_dir(path: str) -> bool:
    try:
        return os.path.isdir(path)
    except OSError:
        return False


def huggingface_cache(environ=None, home=None) -> str:
    """huggingface_hub's cache of downloaded models (``HF_HUB_CACHE``, ``HF_HOME``, else ``~/.cache/huggingface``)."""
    env = os.environ if environ is None else environ
    if env.get("HF_HUB_CACHE"):
        return os.path.expanduser(env["HF_HUB_CACHE"])
    if env.get("HF_HOME"):
        return os.path.join(os.path.expanduser(env["HF_HOME"]), "hub")
    base = env.get("XDG_CACHE_HOME") or os.path.join(home or os.path.expanduser("~"), ".cache")
    return os.path.join(base, "huggingface", "hub")


def is_downloaded(name: str, environ=None, home=None) -> bool:
    """Whether the model is already on disk: a complete faster-whisper snapshot in the Hugging Face cache,
    or a whisper.cpp ``ggml-<name>.bin`` in the engine's models folder. Cheap: a couple of directory lookups."""
    repo = MODEL_REPOS.get(name)
    if repo:
        snapshots = os.path.join(huggingface_cache(environ, home), "models--" + repo.replace("/", "--"), "snapshots")
        if glob.glob(os.path.join(glob.escape(snapshots), "*", "model.bin")):
            return True
    ggml = os.path.join(config_mod.data_dir(environ, home=home), "models", "whispercpp", "ggml-%s.bin" % name)
    return os.path.isfile(ggml)


def catalog(current: str = "", environ=None, home=None) -> list[ModelInfo]:
    """The models to offer, with ``downloaded`` filled in. A ``current`` model that is not in the catalog
    (set by hand in config.json) is kept as an extra entry so it stays selected and unchanged."""
    models = [replace(info, downloaded=is_downloaded(info.name, environ, home)) for info in CATALOG]
    if current and current not in {info.name for info in models}:
        models.append(
            ModelInfo(
                current, "?", "?", "?", "?", "?", "Set by hand in config.json",
                note="Not in the list above; kept as it is unless you pick another model.",
                downloaded=False,
            )
        )  # fmt: skip
    return models


def current_model(config: dict) -> str:
    """The model the next run would use: config.json's ``model``, else the engine's default."""
    value = config.get("model")
    return str(value) if value not in (None, "") else DEFAULT_MODEL


def default_selection(models, current: str) -> int:
    """Position of ``current`` in ``models``; falls back to the engine default, then the first."""
    names = [m.name for m in models]
    for name in (current, DEFAULT_MODEL):
        if name in names:
            return names.index(name)
    return 0


def model_label(info: ModelInfo) -> str:
    label = "%s  –  %s · %s · %s" % (info.name, info.size, info.accuracy, info.languages)
    return "%s  ✔ %s" % (label, DOWNLOADED_MARK) if info.downloaded else label


def model_details(info: ModelInfo) -> str:
    """The detail block shown under the list for the selected model."""
    lines = [
        "Download size: %s%s" % (info.size, "  (already downloaded)" if info.downloaded else ""),
        "Accuracy: %s" % info.accuracy,
        "Speed: %s" % info.speed,
        "Memory: %s" % info.memory,
        "Languages: %s" % info.languages,
        "Best for: %s" % info.best_for,
    ]
    if info.note:
        lines.append(info.note)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# tkinter


class _TkModelDialog:
    def __init__(self, root, models, current):
        import tkinter as tk
        from tkinter import ttk

        self._tk = tk
        self.models = list(models)
        self.result = None
        self.window = tk.Toplevel(root)
        self.window.title("%s – transcription model" % TITLE)
        self.window.protocol("WM_DELETE_WINDOW", self._cancel)
        frame = ttk.Frame(self.window, padding=12)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=HEADING).pack(anchor="w")
        ttk.Label(frame, text=NOTE, foreground="#666").pack(anchor="w", pady=(2, 8))
        self.var = tk.IntVar(value=default_selection(self.models, current))
        for index, info in enumerate(self.models):
            ttk.Radiobutton(frame, text=model_label(info), variable=self.var, value=index).pack(anchor="w")
        self.details_var = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self.details_var, justify="left").pack(anchor="w", pady=(10, 0))
        self.var.trace_add("write", lambda *_args: self._show_details())
        self._show_details()
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(8, 0))
        ttk.Button(buttons, text="Cancel", command=self._cancel).pack(side="right", padx=(8, 0))
        ttk.Button(buttons, text="OK", command=self._ok).pack(side="right")
        self.window.bind("<Return>", lambda _e: self._ok())
        self.window.bind("<Escape>", lambda _e: self._cancel())
        self.window.update_idletasks()

    def _show_details(self):
        self.details_var.set(model_details(self.models[int(self.var.get())]))

    def _ok(self):
        self.result = self.models[int(self.var.get())].name
        self.window.destroy()

    def _cancel(self):
        self.result = None
        self.window.destroy()

    def run(self):
        self.window.grab_set()
        self.window.focus_force()
        self.window.wait_window()
        return self.result


def pick_model_tk(root, models, current) -> str | None:
    """A modal list over ``root``; the chosen model name, or None if cancelled."""
    return _TkModelDialog(root, models, current).run()


# ---------------------------------------------------------------------------
# Fusion UIManager


def pick_model_uimanager(fusion, ui, dispatcher, models, current) -> str | None:
    """The same choice as a UIManager window (``ui`` = ``fusion.UIManager``, ``dispatcher`` = the
    ``UIDispatcher``); the chosen model name, or None if cancelled or closed."""
    models = list(models)
    start = default_selection(models, current)
    window = dispatcher.AddWindow(
        {
            "ID": WINDOW_ID_MODEL,
            "WindowTitle": "%s – transcription model" % TITLE,
            "Geometry": [250, 250, 640, 400],
        },
        [
            ui.VGroup(
                {"Spacing": 6},
                [
                    ui.Label({"Text": HEADING, "Weight": 0}),
                    ui.Label(
                        {"Text": NOTE, "WordWrap": True, "Weight": 0, "MinimumSize": [0, 40]}
                    ),  # wrapped: reserve 2 lines
                    ui.ComboBox({"ID": "ModelChoice", "Weight": 0}),
                    ui.Label({"ID": "ModelDetails", "Text": "", "WordWrap": True, "Weight": 1}),
                    ui.HGroup(
                        {"Weight": 0},
                        [
                            ui.HGap(0, 1),
                            ui.Button({"ID": "Cancel", "Text": "Cancel", "Weight": 0}),
                            ui.Button({"ID": "Ok", "Text": "OK", "Weight": 0}),
                        ],
                    ),
                ],
            )
        ],
    )
    items = window.GetItems()
    combo = items["ModelChoice"]
    for info in models:
        combo.AddItem(model_label(info))
    combo.CurrentIndex = start
    items["ModelDetails"].Text = model_details(models[start])
    state = {"result": None}

    def selected() -> int:
        index = int(combo.CurrentIndex)
        return index if 0 <= index < len(models) else start

    def on_changed(_event=None):
        items["ModelDetails"].Text = model_details(models[selected()])

    def on_ok(_event=None):
        state["result"] = models[selected()].name
        dispatcher.ExitLoop()

    def on_cancel(_event=None):
        dispatcher.ExitLoop()

    window.On.ModelChoice.CurrentIndexChanged = on_changed
    window.On.Ok.Clicked = on_ok
    window.On.Cancel.Clicked = on_cancel
    window.On[WINDOW_ID_MODEL].Close = on_cancel
    window.Show()
    dispatcher.RunLoop()
    window.Hide()
    return state["result"]
