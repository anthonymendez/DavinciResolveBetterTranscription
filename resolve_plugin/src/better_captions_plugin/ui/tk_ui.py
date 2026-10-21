"""tkinter backend (Resolve free, or when UIManager is unavailable)."""

from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk

from better_captions_plugin.ui import Backend, ProgressView, ReviewRequest, ReviewResult

TITLE = "Better Captions"


class TkProgress(ProgressView):
    def __init__(self, root: tk.Tk, title: str):
        self._root = root
        self._cancelled = False
        self.window = tk.Toplevel(root)
        self.window.title(title)
        self.window.resizable(True, False)
        self.window.protocol("WM_DELETE_WINDOW", self._cancel)
        frame = ttk.Frame(self.window, padding=12)
        frame.pack(fill="both", expand=True)
        self._status = tk.StringVar(value="Starting…")
        ttk.Label(frame, textvariable=self._status, width=60).pack(anchor="w")
        self._bar = ttk.Progressbar(frame, length=440, mode="determinate", maximum=100)
        self._bar.pack(fill="x", pady=(6, 6))
        self._log = tk.Text(frame, height=8, width=70, state="disabled", wrap="word")
        self._log.pack(fill="both", expand=True)
        ttk.Button(frame, text="Cancel", command=self._cancel).pack(anchor="e", pady=(8, 0))
        self.window.update_idletasks()
        _center(self.window)
        self.pump()

    def _cancel(self):
        self._cancelled = True
        self._status.set("Cancelling…")

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def set_status(self, text: str):
        self._status.set(text)
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

    def __init__(self, root: "tk.Tk | None" = None):
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

    def review(self, request: ReviewRequest) -> "ReviewResult | None":
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

        ttk.Label(frame, text="Speaker").grid(row=1, column=0, sticky="w")
        ttk.Label(frame, text="Name (track name)").grid(row=1, column=1, sticky="w")
        ttk.Label(frame, text="Text+ template").grid(row=1, column=2, sticky="w")
        ttk.Label(frame, text="Says, e.g.").grid(row=1, column=3, sticky="w")

        self.name_vars = {}
        self.template_boxes = {}
        template_labels = list(request.templates) or ["(no Text+ templates in the media pool)"]
        for row_index, speaker in enumerate(request.speakers, start=2):
            label = "%s (%d cues)" % (speaker.id, speaker.cue_count)
            ttk.Label(frame, text=label).grid(row=row_index, column=0, sticky="w", padx=(0, 8))
            var = tk.StringVar(value=speaker.name)
            self.name_vars[speaker.id] = var
            ttk.Entry(frame, textvariable=var, width=24).grid(row=row_index, column=1, sticky="we", padx=(0, 8))
            box = ttk.Combobox(frame, values=template_labels, state="readonly", width=34)
            box.current(min(row_index - 2, len(template_labels) - 1) if request.templates else 0)
            box.grid(row=row_index, column=2, sticky="we", padx=(0, 8))
            self.template_boxes[speaker.id] = box
            ttk.Label(frame, text=_shorten(speaker.sample, 48), foreground="#666").grid(row=row_index, column=3, sticky="w")

        row_index = len(request.speakers) + 2
        self.subtitles_var = tk.BooleanVar(value=request.default_subtitles and request.allow_subtitles)
        self.textplus_var = tk.BooleanVar(value=request.default_textplus and bool(request.templates))
        ttk.Separator(frame).grid(row=row_index, column=0, columnspan=4, sticky="we", pady=10)
        subtitle_box = ttk.Checkbutton(frame, text="Subtitle tracks: one subtitle track per speaker, named after the speaker",
                                       variable=self.subtitles_var)
        subtitle_box.grid(row=row_index + 1, column=0, columnspan=4, sticky="w")
        if not request.allow_subtitles:
            subtitle_box.state(["disabled"])
        textplus_box = ttk.Checkbutton(frame, text="Text+ captions: one video track per speaker using the chosen template",
                                       variable=self.textplus_var)
        textplus_box.grid(row=row_index + 2, column=0, columnspan=4, sticky="w")
        if not request.templates:
            textplus_box.state(["disabled"])
        ttk.Label(frame, text="New tracks are added; existing tracks are never changed.", foreground="#666").grid(
            row=row_index + 3, column=0, columnspan=4, sticky="w", pady=(6, 0))

        buttons = ttk.Frame(frame)
        buttons.grid(row=row_index + 4, column=0, columnspan=4, sticky="e", pady=(12, 0))
        ttk.Button(buttons, text="Cancel", command=self._cancel).pack(side="right", padx=(8, 0))
        ttk.Button(buttons, text="Create tracks", command=self._ok).pack(side="right")
        self.window.bind("<Return>", lambda _e: self._ok())
        self.window.bind("<Escape>", lambda _e: self._cancel())
        self.window.update_idletasks()
        _center(self.window)

    def _ok(self):
        if not self.subtitles_var.get() and not self.textplus_var.get():
            messagebox.showwarning(TITLE, "Pick at least one output: subtitle tracks and/or Text+ captions.", parent=self.window)
            return
        result = ReviewResult(subtitles=bool(self.subtitles_var.get()), textplus=bool(self.textplus_var.get()))
        for speaker in self.request.speakers:
            result.names[speaker.id] = self.name_vars[speaker.id].get().strip() or speaker.id
            if self.request.templates:
                result.template_index[speaker.id] = int(self.template_boxes[speaker.id].current())
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
