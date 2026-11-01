"""Choose which audio tracks go into the transcription render, and which of them belong to one speaker.

``tracks`` everywhere is ``resolve_io.audio_tracks``'s ``[(index, name, enabled)]``. The two dialogs
(tkinter, Fusion UIManager) follow ui/tk_ui.py and ui/uimanager_ui.py and return a ``TrackPick`` (the
chosen track indices, plus the speaker name typed for any of them), or None when cancelled;
``default_selection`` is the pure part that decides what starts ticked and named. A track with a
speaker name (say "Microphone" = "Anthony") is also rendered alone, and the engine captions what is
said on it as that speaker (``--fixed-track``). tkinter is imported only when the Tk dialog is built,
so UIManager-only installs never need it.
"""

from __future__ import annotations

from better_captions_plugin import config as config_mod

TITLE = "Better Captions"
WINDOW_ID_TRACKS = "BetterCaptionsTracks"
HEADING = "Audio tracks to transcribe"
NOTE = "Only the ticked tracks are rendered for transcription. Your tracks are put back as they were."
SPEAKER_NOTE = (
    "If only one person talks on a track (your microphone, say), type their name: "
    "everything said on it is captioned as them."
)
SPEAKER_PLACEHOLDER = "Speaker (optional)"
NONE_PICKED = "Pick at least one audio track."


class TrackPick(list):
    """The chosen audio track indices (a plain list, in track order), with ``speakers``: {track index:
    speaker name} for the chosen tracks that one named person has to themselves."""

    def __init__(self, indices=(), speakers=None):
        super().__init__(indices)
        chosen = set(self)
        # "=" would split the engine's --fixed-track NAME=PATH in the wrong place
        names = {int(i): " ".join(str(name or "").replace("=", " ").split()) for i, name in (speakers or {}).items()}
        self.speakers = {index: name for index, name in names.items() if index in chosen and name}

    def __repr__(self):
        return "TrackPick(%r, speakers=%r)" % (list(self), self.speakers)


def speakers_of(picked) -> dict[int, str]:
    """``picked.speakers`` for a TrackPick, {} for a plain list of indices (or None)."""
    return dict(getattr(picked, "speakers", None) or {})


def default_selection(config: dict, timeline_name: str, tracks) -> TrackPick:
    """What starts ticked: the tracks picked last time for this timeline (those that still exist), else
    the tracks currently enabled, else all of them; and the speaker names given to them last time."""
    existing = [index for index, _name, _enabled in tracks]
    remembered = config_mod.remembered_audio_tracks(config, timeline_name) or []
    chosen = [index for index in remembered if index in existing]
    if not chosen:
        chosen = [index for index, _name, enabled in tracks if enabled]
    return TrackPick(chosen or existing, config_mod.remembered_track_speakers(config, timeline_name))


def track_label(index: int, name: str, enabled: bool = True) -> str:
    label = "%d  %s" % (index, name) if name else "Audio %d" % index
    return label if enabled else label + "  (disabled now)"


def chosen_indices(tracks, ticked) -> list[int]:
    """The track indices whose ``ticked(index)`` is true, in track order."""
    return [index for index, _name, _enabled in tracks if ticked(index)]


def chosen_pick(tracks, ticked, speaker) -> TrackPick:
    """The ticked tracks (``ticked(index)``) with the speaker name typed for each (``speaker(index)``)."""
    chosen = chosen_indices(tracks, ticked)
    return TrackPick(chosen, {index: speaker(index) or "" for index in chosen})


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
        ttk.Label(frame, text=NOTE, foreground="#666").pack(anchor="w", pady=(2, 0))
        ttk.Label(frame, text=SPEAKER_NOTE, foreground="#666", wraplength=420).pack(anchor="w", pady=(2, 8))
        self.vars = {}
        self.speaker_vars = {}
        speakers = speakers_of(preselected)
        preselected = set(preselected)
        rows = ttk.Frame(frame)
        rows.pack(fill="x")
        rows.columnconfigure(1, weight=1)
        ttk.Label(rows, text=SPEAKER_PLACEHOLDER, foreground="#666").grid(row=0, column=1, sticky="w", padx=(12, 0))
        for row, (index, name, enabled) in enumerate(self.tracks, start=1):
            var = tk.BooleanVar(value=index in preselected)
            self.vars[index] = var
            ttk.Checkbutton(rows, text=track_label(index, name, enabled), variable=var).grid(
                row=row, column=0, sticky="w"
            )
            speaker = tk.StringVar(value=speakers.get(index, ""))
            self.speaker_vars[index] = speaker
            ttk.Entry(rows, textvariable=speaker, width=20).grid(row=row, column=1, sticky="ew", padx=(12, 0))
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
        chosen = chosen_pick(
            self.tracks, lambda index: bool(self.vars[index].get()), lambda index: self.speaker_vars[index].get()
        )
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


def pick_audio_tracks_tk(root, tracks, preselected) -> TrackPick | None:
    """A modal checklist over ``root`` (``preselected`` may be a TrackPick, whose speaker names fill the
    name boxes); the ticked tracks with their speaker names, or None if cancelled."""
    return _TkTrackDialog(root, tracks, preselected).run()


# ---------------------------------------------------------------------------
# Fusion UIManager


def pick_audio_tracks_uimanager(fusion, ui, dispatcher, tracks, preselected) -> TrackPick | None:
    """The same checklist as a UIManager window (``ui`` = ``fusion.UIManager``, ``dispatcher`` = the
    ``UIDispatcher``); the ticked tracks with their speaker names, or None if cancelled or closed."""
    speakers = speakers_of(preselected)
    preselected = set(preselected)
    rows = [
        ui.HGroup(
            {"Weight": 0},
            [
                ui.CheckBox(
                    {
                        "ID": "Track_%d" % index,
                        "Text": track_label(index, name, enabled),
                        "Checked": index in preselected,
                        "Weight": 1,
                    }
                ),
                ui.LineEdit(
                    {
                        "ID": "Speaker_%d" % index,
                        "Text": speakers.get(index, ""),
                        "PlaceholderText": SPEAKER_PLACEHOLDER,
                        "Weight": 1,
                    }
                ),
            ],
        )
        for index, name, enabled in tracks
    ]
    window = dispatcher.AddWindow(
        {
            "ID": WINDOW_ID_TRACKS,
            "WindowTitle": "%s – audio tracks" % TITLE,
            "Geometry": [250, 250, 560, 230 + 32 * len(tracks)],
        },
        [
            ui.VGroup(
                {"Spacing": 6},
                [
                    ui.Label({"Text": HEADING, "Weight": 0}),
                    ui.Label({"Text": NOTE, "WordWrap": True, "Weight": 0}),
                    ui.Label({"Text": SPEAKER_NOTE, "WordWrap": True, "Weight": 0}),
                    *rows,
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
        chosen = chosen_pick(
            tracks,
            lambda index: bool(items["Track_%d" % index].Checked),
            lambda index: str(items["Speaker_%d" % index].Text or ""),
        )
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
