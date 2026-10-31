"""tkinter backend (Resolve free, or when UIManager is unavailable)."""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk

from better_captions_plugin.ui import (
    HIGHLIGHT_COLOR_LABEL,
    HIGHLIGHT_LABEL,
    NAMING_CHOICES,
    NAMING_LABEL,
    PAUSE_LABEL,
    PAUSED_PREFIX,
    RESUME_LABEL,
    STEP_CURRENT,
    STEP_DONE,
    STEP_PENDING,
    Backend,
    ProgressView,
    ReviewRequest,
    ReviewResult,
    checked_highlight_color,
    naming_index,
    speaker_rows,
    step_line,
    step_states,
)
from better_captions_plugin.ui.speaker_editor import EditorController, cue_index

TITLE = "Better Captions"
_STEP_COLORS = {STEP_DONE: "#2e7d32", STEP_CURRENT: "#0b57d0", STEP_PENDING: "#888888"}


class TkProgress(ProgressView):
    def __init__(self, root: tk.Tk, title: str):
        self._root = root
        self._cancelled = False
        self._paused = False
        self._status_text = "Starting…"
        self._step_labels: list[ttk.Label] = []
        self.window = tk.Toplevel(root)
        self.window.title(title)
        self.window.resizable(True, False)
        self.window.protocol("WM_DELETE_WINDOW", self._cancel)
        frame = ttk.Frame(self.window, padding=12)
        frame.pack(fill="both", expand=True)
        self._status = tk.StringVar(value=self._status_text)
        ttk.Label(frame, textvariable=self._status, width=60).pack(anchor="w")
        self._steps_frame = ttk.Frame(frame)
        self._steps_frame.pack(fill="x", pady=(6, 0))
        self._bar = ttk.Progressbar(frame, length=440, mode="determinate", maximum=100)
        self._bar.pack(fill="x", pady=(6, 6))
        self._log = tk.Text(frame, height=8, width=70, state="disabled", wrap="word")
        self._log.pack(fill="both", expand=True)
        buttons = ttk.Frame(frame)
        buttons.pack(anchor="e", pady=(8, 0))
        self._pause_button = ttk.Button(buttons, text=PAUSE_LABEL, command=self._toggle_pause)
        self._pause_button.pack(side="left", padx=(0, 6))
        ttk.Button(buttons, text="Cancel", command=self._cancel).pack(side="left")
        self.window.update_idletasks()
        _center(self.window)
        self.pump()

    def _cancel(self):
        self._cancelled = True
        self._paused = False  # a paused engine has to run again to be stopped
        self._status_text = "Cancelling…"
        self._show_status()

    def _toggle_pause(self):
        if self._cancelled:
            return
        self._paused = not self._paused
        self._pause_button.configure(text=RESUME_LABEL if self._paused else PAUSE_LABEL)
        self._show_status()

    def _show_status(self):
        self._status.set((PAUSED_PREFIX if self._paused else "") + self._status_text)

    def _show_steps(self):
        if len(self._step_labels) != len(self.step_labels):
            for label in self._step_labels:
                label.destroy()
            self._step_labels = [ttk.Label(self._steps_frame) for _ in self.step_labels]
            for label in self._step_labels:
                label.pack(anchor="w")
        states = step_states(len(self.step_labels), self.step_index)
        for widget, text, state in zip(self._step_labels, self.step_labels, states):
            widget.configure(text=step_line(text, state), foreground=_STEP_COLORS[state])
        self.pump()

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    @property
    def paused(self) -> bool:
        return self._paused

    def set_status(self, text: str):
        self._status_text = text
        self._show_status()
        self.pump()

    def set_progress(self, pct):
        if pct is None:
            self._bar.configure(mode="indeterminate")
        else:
            self._bar.configure(mode="determinate")
            self._bar["value"] = max(0.0, min(100.0, float(pct)))
        self.pump()

    def log(self, line: str):
        self._log.configure(state="normal")
        self._log.insert("end", str(line).rstrip("\n") + "\n")
        self._log.see("end")
        self._log.configure(state="disabled")
        self.pump()

    def pump(self):
        try:
            self._root.update()
        except tk.TclError:
            self._cancelled = True

    def close(self):
        try:
            self.window.destroy()
            self._root.update()
        except tk.TclError:
            pass


class TkBackend(Backend):
    name = "tk"

    def __init__(self, root: tk.Tk | None = None):
        self.root = root or tk.Tk()
        self.root.withdraw()
        try:
            self.root.attributes("-topmost", True)
        except tk.TclError:
            pass

    def progress(self, title: str) -> ProgressView:
        return TkProgress(self.root, title)

    def message(self, title: str, text: str):
        self.root.update()
        messagebox.showinfo(title, text, parent=self.root)

    def error(self, title: str, text: str):
        self.root.update()
        messagebox.showerror(title, text, parent=self.root)

    def pick_audio_tracks(self, tracks, preselected) -> list[int] | None:
        from better_captions_plugin import track_picker

        return track_picker.pick_audio_tracks_tk(self.root, tracks, preselected)

    def pick_model(self, models, current: str) -> str | None:
        from better_captions_plugin import model_picker

        return model_picker.pick_model_tk(self.root, models, current)

    def review(self, request: ReviewRequest) -> ReviewResult | None:
        dialog = _ReviewDialog(self.root, request)
        return dialog.run()

    def close(self):
        try:
            self.root.destroy()
        except tk.TclError:
            pass


class _ReviewDialog:
    def __init__(self, root: tk.Tk, request: ReviewRequest):
        self.request = request
        self.result = None
        self.speakers = list(request.speakers)
        self.window = tk.Toplevel(root)
        self.window.title("%s – review speakers" % TITLE)
        self.window.protocol("WM_DELETE_WINDOW", self._cancel)
        frame = ttk.Frame(self.window, padding=12)
        frame.pack(fill="both", expand=True)

        header = "Timeline: %s" % request.timeline_name if request.timeline_name else ""
        if request.range_label:
            header += "   Range: %s" % request.range_label
        if header:
            ttk.Label(frame, text=header).grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 8))
        if request.note:
            ttk.Label(frame, text=request.note, foreground="#666").grid(
                row=1, column=0, columnspan=4, sticky="w", pady=(0, 8)
            )

        self.table = ttk.Frame(frame)
        self.table.grid(row=2, column=0, columnspan=4, sticky="we")
        self.name_vars = {}
        self.template_boxes = {}
        self._build_rows({})

        row_index = 3
        self.subtitles_var = tk.BooleanVar(value=request.default_subtitles and request.allow_subtitles)
        self.textplus_var = tk.BooleanVar(value=request.default_textplus and bool(request.templates))
        ttk.Separator(frame).grid(row=row_index, column=0, columnspan=4, sticky="we", pady=10)
        subtitle_box = ttk.Checkbutton(
            frame,
            text="Subtitle tracks: one subtitle track per speaker, named after the speaker",
            variable=self.subtitles_var,
        )
        subtitle_box.grid(row=row_index + 1, column=0, columnspan=4, sticky="w")
        if not request.allow_subtitles:
            subtitle_box.state(["disabled"])
        textplus_box = ttk.Checkbutton(
            frame,
            text="Text+ captions: one video track per speaker using the chosen template",
            variable=self.textplus_var,
        )
        textplus_box.grid(row=row_index + 2, column=0, columnspan=4, sticky="w")
        if not request.templates:
            textplus_box.state(["disabled"])
        highlight_row = ttk.Frame(frame)
        highlight_row.grid(row=row_index + 3, column=0, columnspan=4, sticky="w", padx=(20, 0))
        self.highlight_var = tk.BooleanVar(value=bool(request.highlight_words))
        self.highlight_color_var = tk.StringVar(value=request.highlight_color)
        self.highlight_widgets = [
            ttk.Checkbutton(highlight_row, text=HIGHLIGHT_LABEL, variable=self.highlight_var),
            ttk.Label(highlight_row, text=HIGHLIGHT_COLOR_LABEL),
            ttk.Entry(highlight_row, textvariable=self.highlight_color_var, width=9),
            ttk.Button(highlight_row, text="Pick…", command=self._pick_color),
        ]
        for column, widget in enumerate(self.highlight_widgets):
            widget.grid(row=0, column=column, sticky="w", padx=(0 if column == 0 else 6, 0))
        self.textplus_var.trace_add("write", lambda *_args: self._sync_highlight_state())
        self._sync_highlight_state()
        ttk.Label(frame, text="New tracks are added; existing tracks are never changed.", foreground="#666").grid(
            row=row_index + 4, column=0, columnspan=4, sticky="w", pady=(6, 0)
        )
        naming_row = ttk.Frame(frame)
        naming_row.grid(row=row_index + 5, column=0, columnspan=4, sticky="w", pady=(10, 0))
        ttk.Label(naming_row, text=NAMING_LABEL).pack(side="left")
        self.naming_box = ttk.Combobox(
            naming_row, values=[label for _value, label in NAMING_CHOICES], state="readonly", width=60
        )
        self.naming_box.current(naming_index(request.naming_provider))
        self.naming_box.pack(side="left", padx=(6, 0))

        buttons = ttk.Frame(frame)
        buttons.grid(row=row_index + 6, column=0, columnspan=4, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="Cancel", command=self._cancel).pack(side="right", padx=(8, 0))
        ttk.Button(buttons, text="Create tracks", command=self._ok).pack(side="right")
        if request.editor is not None:
            ttk.Button(buttons, text="Fix speakers…", command=self._fix_speakers).pack(side="right", padx=(0, 24))
        self.window.bind("<Return>", lambda _e: self._ok())
        self.window.bind("<Escape>", lambda _e: self._cancel())
        self.window.update_idletasks()
        _center(self.window)

    def _build_rows(self, template_choice: dict[str, int]):
        """(Re)build one row per speaker in ``self.speakers``; template picks carry over by speaker id."""
        for child in self.table.winfo_children():
            child.destroy()
        ttk.Label(self.table, text="Speaker").grid(row=0, column=0, sticky="w")
        ttk.Label(self.table, text="Name (track name)").grid(row=0, column=1, sticky="w")
        ttk.Label(self.table, text="Text+ template").grid(row=0, column=2, sticky="w")
        ttk.Label(self.table, text="Says, e.g.").grid(row=0, column=3, sticky="w")
        self.name_vars = {}
        self.template_boxes = {}
        template_labels = list(self.request.templates) or ["(no Text+ templates in the media pool)"]
        for row_index, speaker in enumerate(self.speakers, start=1):
            label = "%s (%d cues)" % (speaker.id, speaker.cue_count)
            ttk.Label(self.table, text=label).grid(row=row_index, column=0, sticky="w", padx=(0, 8))
            var = tk.StringVar(value=speaker.name)
            self.name_vars[speaker.id] = var
            ttk.Entry(self.table, textvariable=var, width=24).grid(row=row_index, column=1, sticky="we", padx=(0, 8))
            box = ttk.Combobox(self.table, values=template_labels, state="readonly", width=34)
            default = min(row_index - 1, len(template_labels) - 1) if self.request.templates else 0
            box.current(template_choice.get(speaker.id, default))
            box.grid(row=row_index, column=2, sticky="we", padx=(0, 8))
            self.template_boxes[speaker.id] = box
            ttk.Label(self.table, text=_shorten(speaker.sample, 48), foreground="#666").grid(
                row=row_index, column=3, sticky="w"
            )

    def _fix_speakers(self):
        """Open the speaker editor; names typed so far go in as renames, the rows are rebuilt after."""
        editor = self.request.editor
        editor.record_names({sid: var.get() for sid, var in self.name_vars.items()})
        choices = {sid: _current(box) for sid, box in self.template_boxes.items()}
        template_choice = {sid: index for sid, index in choices.items() if index >= 0}
        _SpeakerEditorDialog(self.window, EditorController(editor, self.request.time_label)).run()
        self.speakers = speaker_rows(editor)
        self._build_rows(template_choice)
        try:
            self.window.grab_set()  # the editor took the grab
        except tk.TclError:
            pass

    def _sync_highlight_state(self):
        """Highlighting only applies to Text+ captions: usable while that output is ticked."""
        state = ["!disabled"] if self.textplus_var.get() and self.request.templates else ["disabled"]
        for widget in self.highlight_widgets:
            widget.state(state)

    def _pick_color(self):
        from tkinter import colorchooser

        initial = checked_highlight_color(self.highlight_color_var.get()) or self.request.highlight_color
        _rgb, chosen = colorchooser.askcolor(color=initial, parent=self.window, title="Highlight colour")
        if chosen:
            self.highlight_color_var.set(checked_highlight_color(chosen) or chosen)

    def _ok(self):
        if not self.subtitles_var.get() and not self.textplus_var.get():
            messagebox.showwarning(
                TITLE, "Pick at least one output: subtitle tracks and/or Text+ captions.", parent=self.window
            )
            return
        color = checked_highlight_color(self.highlight_color_var.get())
        if self.highlight_var.get() and self.textplus_var.get() and color is None:
            messagebox.showwarning(TITLE, "The highlight colour must look like #FFD400.", parent=self.window)
            return
        result = ReviewResult(
            subtitles=bool(self.subtitles_var.get()),
            textplus=bool(self.textplus_var.get()),
            highlight_words=bool(self.highlight_var.get()),
            highlight_color=color or self.request.highlight_color,
            naming_provider=NAMING_CHOICES[max(0, _current(self.naming_box))][0],
        )
        for speaker in self.speakers:
            result.names[speaker.id] = self.name_vars[speaker.id].get().strip() or speaker.id
            if self.request.templates:
                result.template_index[speaker.id] = _current(self.template_boxes[speaker.id])
        self.result = result
        self.window.destroy()

    def _cancel(self):
        self.result = None
        self.window.destroy()

    def run(self):
        self.window.grab_set()
        self.window.focus_force()
        self.window.wait_window()
        return self.result


class _SpeakerEditorDialog:
    """Fix speakers: rename, merge into…, move selected cues to another or a new speaker, undo."""

    def __init__(self, parent, controller: EditorController):
        self.controller = controller
        self._choice_ids = {}  # combobox -> ids parallel to its values
        self.window = tk.Toplevel(parent)
        self.window.title("%s – fix speakers" % TITLE)
        self.window.protocol("WM_DELETE_WINDOW", self._close)
        frame = ttk.Frame(self.window, padding=12)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(0, weight=1)

        left = ttk.LabelFrame(frame, text="Speakers", padding=8)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        left.columnconfigure(1, weight=1)
        self.speaker_tree = ttk.Treeview(
            left, columns=("id", "name", "cues"), show="headings", selectmode="browse", height=8
        )
        for column, title, width in (("id", "Speaker", 110), ("name", "Name", 140), ("cues", "Cues", 50)):
            self.speaker_tree.heading(column, text=title)
            self.speaker_tree.column(column, width=width, stretch=column == "name")
        self.speaker_tree.grid(row=0, column=0, columnspan=3, sticky="nsew")
        self.speaker_tree.bind("<<TreeviewSelect>>", lambda _e: self._on_speaker_selected())
        self.name_var = tk.StringVar()
        ttk.Label(left, text="Name:").grid(row=1, column=0, sticky="w", pady=(8, 0))
        name_entry = ttk.Entry(left, textvariable=self.name_var, width=20)
        name_entry.grid(row=1, column=1, sticky="we", pady=(8, 0))
        name_entry.bind("<Return>", lambda _e: self._rename())
        ttk.Button(left, text="Rename", command=self._rename).grid(
            row=1, column=2, sticky="e", padx=(6, 0), pady=(8, 0)
        )
        ttk.Label(left, text="Merge into:").grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.merge_box = ttk.Combobox(left, state="readonly", width=20)
        self.merge_box.grid(row=2, column=1, sticky="we", pady=(6, 0))
        ttk.Button(left, text="Merge", command=self._merge).grid(row=2, column=2, sticky="e", padx=(6, 0), pady=(6, 0))
        ttk.Label(
            left,
            text="Merge moves all of the selected speaker's cues;\nthe speaker merged into keeps its name.",
            foreground="#666",
        ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(6, 0))

        right = ttk.LabelFrame(frame, text="Cues", padding=8)
        right.grid(row=0, column=1, sticky="nsew")
        right.columnconfigure(1, weight=1)
        right.rowconfigure(1, weight=1)
        ttk.Label(right, text="Show:").grid(row=0, column=0, sticky="w")
        self.filter_box = ttk.Combobox(right, state="readonly", width=30)
        self.filter_box.grid(row=0, column=1, sticky="w")
        self.filter_box.bind("<<ComboboxSelected>>", lambda _e: self._refresh_cues())
        self.cue_tree = ttk.Treeview(
            right, columns=("num", "time", "speaker", "text"), show="headings", selectmode="extended", height=14
        )
        for column, title, width in (
            ("num", "#", 50),
            ("time", "Time", 100),
            ("speaker", "Speaker", 120),
            ("text", "Text", 360),
        ):
            self.cue_tree.heading(column, text=title)
            self.cue_tree.column(column, width=width, stretch=column == "text")
        scroll = ttk.Scrollbar(right, orient="vertical", command=self.cue_tree.yview)
        self.cue_tree.configure(yscrollcommand=scroll.set)
        self.cue_tree.grid(row=1, column=0, columnspan=3, sticky="nsew", pady=(6, 0))
        scroll.grid(row=1, column=3, sticky="ns", pady=(6, 0))
        ttk.Label(right, text="Move selected to:").grid(row=2, column=0, sticky="w", pady=(6, 0))
        self.move_box = ttk.Combobox(right, state="readonly", width=30)
        self.move_box.grid(row=2, column=1, sticky="w", pady=(6, 0))
        ttk.Button(right, text="Move", command=self._move).grid(row=2, column=2, sticky="e", pady=(6, 0))

        bottom = ttk.Frame(frame)
        bottom.grid(row=1, column=0, columnspan=2, sticky="we", pady=(10, 0))
        self.undo_button = ttk.Button(bottom, text="Undo", command=self._undo)
        self.undo_button.pack(side="left")
        self.status_var = tk.StringVar()
        ttk.Label(bottom, textvariable=self.status_var).pack(side="left", padx=(10, 0))
        ttk.Button(bottom, text="Done", command=self._close).pack(side="right")
        self.window.bind("<Escape>", lambda _e: self._close())
        self.window.bind("<Control-z>", lambda _e: self._undo())
        self._refresh()
        _center(self.window)

    # selection

    def selected_speaker(self) -> str | None:
        selection = self.speaker_tree.selection()
        return selection[0] if selection else None

    def selected_cues(self) -> list[int]:
        return [cue_index(iid) for iid in self.cue_tree.selection()]

    def _choice(self, box) -> str | None:
        ids = self._choice_ids.get(str(box), [])
        index = _current(box)
        return ids[index] if 0 <= index < len(ids) else None

    def _set_choices(self, box, choices, keep=None):
        ids = [value for _label, value in choices]
        self._choice_ids[str(box)] = ids
        box["values"] = [label for label, _value in choices]
        if ids:
            box.current(ids.index(keep) if keep in ids else 0)
        else:
            box.set("")

    # refresh

    def _refresh(self):
        controller = self.controller
        selected = self.selected_speaker()
        self.speaker_tree.delete(*self.speaker_tree.get_children())
        for speaker_id, name, count in controller.speaker_rows():
            self.speaker_tree.insert("", "end", iid=speaker_id, values=(speaker_id, name, count))
        if selected in controller.speaker_ids():
            self.speaker_tree.selection_set(selected)
        self._set_choices(self.filter_box, controller.filter_choices(), keep=self._choice(self.filter_box))
        self._set_choices(self.move_box, controller.move_choices(), keep=self._choice(self.move_box))
        self._on_speaker_selected()
        self._refresh_cues()
        self.undo_button.state(["!disabled"] if controller.can_undo else ["disabled"])
        self.status_var.set(controller.status)

    def _refresh_cues(self):
        self.cue_tree.delete(*self.cue_tree.get_children())
        for row in self.controller.cue_rows(self._choice(self.filter_box)):
            self.cue_tree.insert("", "end", iid=row[0], values=row)

    def _on_speaker_selected(self):
        speaker_id = self.selected_speaker()
        self.name_var.set(self.controller.editor.display_name(speaker_id) if speaker_id else "")
        self._set_choices(self.merge_box, self.controller.merge_choices(speaker_id), keep=self._choice(self.merge_box))

    # actions

    def _rename(self):
        self.controller.rename(self.selected_speaker(), self.name_var.get())
        self._refresh()

    def _merge(self):
        self.controller.merge(self.selected_speaker(), self._choice(self.merge_box))
        self._refresh()

    def _move(self):
        self.controller.move(self.selected_cues(), self._choice(self.move_box))
        self._refresh()

    def _undo(self):
        self.controller.undo()
        self._refresh()

    def _close(self):
        self.window.destroy()

    def run(self):
        self.window.grab_set()
        self.window.focus_force()
        self.window.wait_window()


def _current(box) -> int:
    """``box.current()``, or -1 when nothing is chosen. Tk 9 answers "" there instead of Tk 8.6's -1,
    and tkinter before Python 3.12 passes that "" to getint, which raises TclError."""
    try:
        return int(box.current())
    except (tk.TclError, ValueError):
        return -1


def _center(window):
    try:
        window.update_idletasks()
        width, height = window.winfo_width(), window.winfo_height()
        x = (window.winfo_screenwidth() - width) // 2
        y = (window.winfo_screenheight() - height) // 3
        window.geometry("+%d+%d" % (max(0, x), max(0, y)))
    except tk.TclError:
        pass


def _shorten(text: str, limit: int) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"
