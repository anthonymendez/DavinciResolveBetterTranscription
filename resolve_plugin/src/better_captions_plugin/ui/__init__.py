"""UI backends. Resolve Studio gets Fusion's UIManager; the free edition (UIManager is
Studio-only since Resolve 19.1, see docs/RESOLVE_INTEGRATION.md) gets tkinter, which ships
with python.org's Windows and macOS installers. ``app`` only talks to the ``Backend`` API."""

from __future__ import annotations

from dataclasses import dataclass, field

from better_captions_plugin import compat
from better_captions_plugin.word_highlight import DEFAULT_HIGHLIGHT_COLOR, normalize_hex_color

HIGHLIGHT_LABEL = "Highlight words as they're spoken (Text+ captions; one clip per word)"
HIGHLIGHT_COLOR_LABEL = "Highlight colour:"
NAMING_LABEL = "Speaker naming (next run):"
# Engine ``--naming`` values and their labels, in the order shown.
NAMING_CHOICES = (
    ("auto", "Automatic (API key, then Claude Code / Antigravity CLI, then rules)"),
    ("heuristic", "Rules only (nothing leaves this computer)"),
    ("claude-cli", "Claude Code CLI (your Anthropic login)"),
    ("antigravity-cli", "Antigravity CLI (your Google login)"),
    ("anthropic", "Anthropic API key"),
    ("openai", "OpenAI-compatible API (OpenAI, Ollama, LM Studio)"),
    ("none", "Off (keep SPEAKER_00, ...)"),
)


def naming_index(provider) -> int:
    """Position of ``provider`` in NAMING_CHOICES; unknown values show as "auto"."""
    values = [value for value, _label in NAMING_CHOICES]
    return values.index(provider) if provider in values else 0


def checked_highlight_color(text: str) -> str | None:
    """The colour typed in the review dialog as ``#RRGGBB``, or None when it isn't one."""
    try:
        return normalize_hex_color(text)
    except ValueError:
        return None


@dataclass
class SpeakerRow:
    id: str
    name: str  # suggested name (engine's guess or the id)
    cue_count: int
    sample: str  # a short example of what they say
    source: str | None = None  # "heuristic", "llm", ...


@dataclass
class ReviewRequest:
    speakers: list[SpeakerRow]
    templates: list[str]  # labels, in order; indexes are what ReviewResult refers to
    timeline_name: str = ""
    range_label: str = ""
    allow_subtitles: bool = True
    default_subtitles: bool = True
    default_textplus: bool = False
    # speaker_edits.SpeakerEditor: when set, the dialog offers "Fix speakers…" and, after
    # edits, rebuilds its rows from it (``speaker_rows``); names typed so far go in as renames.
    editor: object = None
    time_label: object = None  # callable(seconds) -> str for the cue list (e.g. timeline timecode)
    note: str = ""  # shown under the header, e.g. "re-applied 3 earlier speaker fixes"
    # Word highlighting (Text+ only), as remembered in config.json.
    highlight_words: bool = False
    highlight_color: str = DEFAULT_HIGHLIGHT_COLOR
    # Speaker naming provider for the next engine run, as remembered in config.json.
    naming_provider: str = "auto"


def speaker_rows(editor) -> list[SpeakerRow]:
    """Review rows for the editor's current speakers."""
    return [
        SpeakerRow(id=s.id, name=s.name, cue_count=s.cue_count, sample=s.sample, source=s.source)
        for s in editor.speakers()
    ]


@dataclass
class ReviewResult:
    names: dict[str, str] = field(default_factory=dict)  # speaker id -> name
    subtitles: bool = True
    textplus: bool = False
    template_index: dict[str, int] = field(default_factory=dict)  # speaker id -> index into templates
    highlight_words: bool = False
    highlight_color: str = DEFAULT_HIGHLIGHT_COLOR  # "#RRGGBB"
    naming_provider: str | None = None  # a NAMING_CHOICES value; None leaves config.json's as is


STEP_DONE = "done"
STEP_CURRENT = "current"
STEP_PENDING = "pending"
STEP_MARKERS = {STEP_DONE: "✔", STEP_CURRENT: "▶", STEP_PENDING: "○"}
PAUSE_LABEL = "Pause"
RESUME_LABEL = "Resume"
PAUSED_PREFIX = "Paused – "


def step_states(count: int, current: int | None) -> list[str]:
    """Marker state per step: those before ``current`` are done, ``current`` is current, the rest pending.
    ``current`` None leaves every step pending; at or past ``count`` every step is done."""
    if current is None:
        return [STEP_PENDING] * count
    return [STEP_DONE if i < current else STEP_CURRENT if i == current else STEP_PENDING for i in range(count)]


def step_line(label: str, state: str) -> str:
    return "%s  %s" % (STEP_MARKERS[state], label)


class ProgressView:
    """A modal-ish progress window. ``pump()`` must be cheap and safe to call often.

    ``set_steps`` / ``set_step`` show the whole list of steps with done / current / pending markers;
    the Pause button flips ``paused`` (the flow checks it, the window doesn't stop anything itself)."""

    step_labels: tuple = ()
    step_index: int | None = None

    def set_steps(self, labels):
        """Show these step labels, all pending."""
        self.step_labels = tuple(str(label) for label in labels)
        self.step_index = None
        self._show_steps()

    def set_step(self, index: int):
        """Step ``index`` is now running; the ones before it are done. ``len(steps)`` marks every step done."""
        self.step_index = index
        self._show_steps()

    def step_lines(self) -> list[str]:
        states = step_states(len(self.step_labels), self.step_index)
        return [step_line(label, state) for label, state in zip(self.step_labels, states)]

    def _show_steps(self):
        """Redraw the step list; backends override this."""

    @property
    def paused(self) -> bool:
        return False

    def set_status(self, text: str):
        raise NotImplementedError

    def set_progress(self, pct: float | None):
        raise NotImplementedError

    def log(self, line: str):
        raise NotImplementedError

    def pump(self):
        raise NotImplementedError

    @property
    def cancelled(self) -> bool:
        return False

    def close(self):
        raise NotImplementedError


class Backend:
    name = "base"

    def progress(self, title: str) -> ProgressView:
        raise NotImplementedError

    def review(self, request: ReviewRequest) -> ReviewResult | None:
        """Return None when the user cancels."""
        raise NotImplementedError

    def pick_audio_tracks(self, tracks, preselected) -> list[int] | None:
        """The audio tracks to render (``tracks`` as ``resolve_io.audio_tracks`` gives them); None cancels."""
        return list(preselected)

    def message(self, title: str, text: str):
        raise NotImplementedError

    def error(self, title: str, text: str):
        self.message(title, text)

    def close(self):
        pass


def choose_backend(
    resolve=None, fusion=None, bmd=None, preference: str = "auto", studio: bool | None = None
) -> Backend:
    """``preference``: "auto" (UIManager on Studio, tkinter otherwise), "uimanager" or "tk"."""
    preference = (preference or "auto").lower()
    if studio is None:
        studio = compat.detect(resolve).studio
    if preference in ("uimanager", "fusion") or (preference == "auto" and studio and fusion is not None):
        try:
            from better_captions_plugin.ui.uimanager_ui import UIManagerBackend

            return UIManagerBackend(fusion, bmd)
        except Exception as exc:
            if preference != "auto":
                raise RuntimeError("UIManager UI unavailable: %s" % exc) from exc
    from better_captions_plugin.ui.tk_ui import TkBackend

    return TkBackend()
