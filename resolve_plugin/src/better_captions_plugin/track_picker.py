"""Choose which audio tracks go into the transcription render.

``tracks`` everywhere is ``resolve_io.audio_tracks``'s ``[(index, name, enabled)]``. The two dialogs
(tkinter, Fusion UIManager) follow ui/tk_ui.py and ui/uimanager_ui.py and return the chosen track
indices, or None when cancelled; ``default_selection`` is the pure part that decides what starts ticked.
tkinter is imported only when the Tk dialog is built, so UIManager-only installs never need it.
"""

from __future__ import annotations

from better_captions_plugin import config as config_mod

TITLE = "Better Captions"
WINDOW_ID_TRACKS = "BetterCaptionsTracks"
HEADING = "Audio tracks to transcribe"
NOTE = "Only the ticked tracks are rendered for transcription. Your tracks are put back as they were."
NONE_PICKED = "Pick at least one audio track."


def default_selection(config: dict, timeline_name: str, tracks) -> list[int]:
    """What starts ticked: the tracks picked last time for this timeline (those that still exist), else
    the tracks currently enabled, else all of them."""
    existing = [index for index, _name, _enabled in tracks]
    remembered = config_mod.remembered_audio_tracks(config, timeline_name) or []
    chosen = [index for index in remembered if index in existing]
    if not chosen:
        chosen = [index for index, _name, enabled in tracks if enabled]
    return chosen or existing


def track_label(index: int, name: str, enabled: bool = True) -> str:
    label = "%d  %s" % (index, name) if name else "Audio %d" % index
    return label if enabled else label + "  (disabled now)"


def chosen_indices(tracks, ticked) -> list[int]:
    """The track indices whose ``ticked(index)`` is true, in track order."""
    return [index for index, _name, _enabled in tracks if ticked(index)]


# ---------------------------------------------------------------------------
# tkinter


class _TkTrackDialog:
    def __init__(self, root, tracks, preselected):
        import tkinter as tk
        from tkinter import ttk

        self._tk = tk
        self.tracks = list(tracks)
        self.result = None
        self.window = tk.Toplevel(root)
        self.window.title("%s – audio tracks" % TITLE)
        self.window.protocol("WM_DELETE_WINDOW", self._cancel)
        frame = ttk.Frame(self.window, padding=12)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text=HEADING).pack(anchor="w")
        ttk.Label(frame, text=NOTE, foreground="#666").pack(anchor="w", pady=(2, 8))
        self.vars = {}
        preselected = set(preselected)
        for index, name, enabled in self.tracks:
            var = tk.BooleanVar(value=index in preselected)
            self.vars[index] = var
            ttk.Checkbutton(frame, text=track_label(index, name, enabled), variable=var).pack(anchor="w")
        self.status_var = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self.status_var, foreground="#b00").pack(anchor="w", pady=(6, 0))
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(8, 0))
        ttk.Button(buttons, text="Cancel", command=self._cancel).pack(side="right", padx=(8, 0))
        ttk.Button(buttons, text="OK", command=self._ok).pack(side="right")
        self.window.bind("<Return>", lambda _e: self._ok())
        self.window.bind("<Escape>", lambda _e: self._cancel())
        self.window.update_idletasks()

    def _ok(self):
        chosen = chosen_indices(self.tracks, lambda index: bool(self.vars[index].get()))
        if not chosen:
            self.status_var.set(NONE_PICKED)
            return
        self.result = chosen
        self.window.destroy()

    def _cancel(self):
        self.result = None
        self.window.destroy()

    def run(self):
        self.window.grab_set()
        self.window.focus_force()
        self.window.wait_window()
        return self.result


def pick_audio_tracks_tk(root, tracks, preselected) -> list[int] | None:
    """A modal checklist over ``root``; the ticked track indices, or None if cancelled."""
    return _TkTrackDialog(root, tracks, preselected).run()


# ---------------------------------------------------------------------------
# Fusion UIManager


def pick_audio_tracks_uimanager(fusion, ui, dispatcher, tracks, preselected) -> list[int] | None:
    """The same checklist as a UIManager window (``ui`` = ``fusion.UIManager``, ``dispatcher`` = the
    ``UIDispatcher``); the ticked track indices, or None if cancelled or closed."""
    preselected = set(preselected)
    boxes = [
        ui.CheckBox(
            {
                "ID": "Track_%d" % index,
                "Text": track_label(index, name, enabled),
                "Checked": index in preselected,
                "Weight": 0,
            }
        )
        for index, name, enabled in tracks
    ]
    window = dispatcher.AddWindow(
        {
            "ID": WINDOW_ID_TRACKS,
            "WindowTitle": "%s – audio tracks" % TITLE,
            "Geometry": [250, 250, 460, 190 + 28 * len(tracks)],
        },
        [
            ui.VGroup(
                {"Spacing": 6},
                [
                    ui.Label({"Text": HEADING, "Weight": 0}),
                    ui.Label({"Text": NOTE, "WordWrap": True, "Weight": 0}),
                    *boxes,
                    ui.Label({"ID": "TracksStatus", "Text": "", "Weight": 0}),
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
    state = {"result": None}

    def on_ok(_event=None):
        chosen = chosen_indices(tracks, lambda index: bool(items["Track_%d" % index].Checked))
        if not chosen:
            items["TracksStatus"].Text = NONE_PICKED
            return
        state["result"] = chosen
        dispatcher.ExitLoop()

    def on_cancel(_event=None):
        dispatcher.ExitLoop()

    window.On.Ok.Clicked = on_ok
    window.On.Cancel.Clicked = on_cancel
    window.On[WINDOW_ID_TRACKS].Close = on_cancel
    window.Show()
    dispatcher.RunLoop()
    window.Hide()
    return state["result"]
