"""Choose which audio tracks to transcribe, and who talks on each of them.

``tracks`` everywhere is ``resolve_io.audio_tracks``'s ``[(index, name, enabled)]``. The two dialogs
(tkinter, Fusion UIManager) follow ui/tk_ui.py and ui/uimanager_ui.py and return a ``TrackPick`` (the
chosen track indices, with a role for each and the speaker name typed for any of them), or None when
cancelled; ``default_selection`` is the pure part that decides what starts ticked, named and in which role.

Each ticked track has a role (``ROLES``):

* one person (``ROLE_PERSON``, needs their name, say "Microphone" = "Anthony"): everything said on it is
  captioned as them;
* several people (``ROLE_GROUP``, a Discord call): speaker detection runs on this track alone;
* not speech (``ROLE_NONE``, game audio, music): it is not transcribed.

Every speech track is rendered alone and passed to the engine as ``--track ROLE:NAME=PATH``
(``speech_tracks``). By default a named track is one person, a track named like game audio or music is
not speech, and any other track is several people. tkinter is imported only when the Tk dialog is built,
so UIManager-only installs never need it.
"""

from __future__ import annotations

import re

from better_captions_plugin import config as config_mod

TITLE = "Better Captions"
WINDOW_ID_TRACKS = "BetterCaptionsTracks"
HEADING = "Audio tracks to transcribe"
NOTE = (
    "Each ticked speech track is rendered and transcribed on its own, so people talking at once are all "
    "captioned. Your tracks are put back as they were."
)
SPEAKER_NOTE = (
    'If only one person talks on a track (your microphone, say), pick "One person" and type their name: '
    "everything said on it is captioned as them."
)
SPEAKER_PLACEHOLDER = "Name (one person)"
ROLE_HEADING = "Who talks on it"
NOTE_HEIGHT = 40  # px: two lines of a wrapped note in the UIManager dialog
NONE_PICKED = "Pick at least one audio track."
NO_SPEECH = 'Pick at least one track with speech ("One person" or "Several people").'
NAME_MISSING = 'Type the name of the person on track %d, or pick "Several people".'

ROLE_PERSON = "person"
ROLE_GROUP = "group"
ROLE_NONE = "none"
ROLES = (ROLE_PERSON, ROLE_GROUP, ROLE_NONE)  # the order of the dialogs' role choices
ROLE_LABELS = {ROLE_PERSON: "One person", ROLE_GROUP: "Several people", ROLE_NONE: "Not speech"}
# a track whose name has a word starting with one of these is not speech by default
NOT_SPEECH_WORDS = ("game", "music", "song", "sfx", "fx", "effect", "bgm", "soundtrack", "ambien", "background")


def clean_name(name) -> str:
    """A name as the engine's ``--track ROLE:NAME=PATH`` takes it: one line, no "=" (it would split there)."""
    return " ".join(str(name or "").replace("=", " ").split())


def default_role(track_name, speaker="") -> str:
    """The role a track starts with: one person when a name is typed for it, not speech when it is named
    like game audio or music, else several people."""
    if clean_name(speaker):
        return ROLE_PERSON
    words = re.split(r"[^a-z0-9]+", str(track_name or "").lower())
    if any(word.startswith(NOT_SPEECH_WORDS) for word in words if word):
        return ROLE_NONE
    return ROLE_GROUP


class TrackPick(list):
    """The chosen audio track indices (a plain list, in track order), with ``speakers``: {track index:
    speaker name} for the chosen tracks given a name, and ``roles``: {track index: role} as chosen (it
    may also cover tracks not chosen, as a preselection); ``role_of`` fills in the rest."""

    def __init__(self, indices=(), speakers=None, roles=None):
        super().__init__(indices)
        chosen = set(self)
        names = {int(i): clean_name(name) for i, name in (speakers or {}).items()}
        self.speakers = {index: name for index, name in names.items() if index in chosen and name}
        self.roles = {int(i): str(role) for i, role in (roles or {}).items() if str(role) in ROLES}

    def role_of(self, index: int, track_name: str = "") -> str:
        """The role of track ``index``: the one chosen (one person only with a name), else ``default_role``."""
        speaker = self.speakers.get(index, "")
        role = self.roles.get(index)
        if role == ROLE_PERSON and not speaker:
            return ROLE_GROUP
        return role or default_role(track_name, speaker)

    def __repr__(self):
        return "TrackPick(%r, speakers=%r, roles=%r)" % (list(self), self.speakers, self.roles)


def speakers_of(picked) -> dict[int, str]:
    """``picked.speakers`` for a TrackPick, {} for a plain list of indices (or None)."""
    return dict(getattr(picked, "speakers", None) or {})


def roles_of(picked) -> dict[int, str]:
    """``picked.roles`` for a TrackPick, {} for a plain list of indices (or None)."""
    return dict(getattr(picked, "roles", None) or {})


def complete(tracks, picked) -> TrackPick:
    """``picked`` (a TrackPick or a plain list of indices) as a TrackPick with the role of every chosen track
    settled (``TrackPick.role_of``, from the track names in ``tracks``)."""
    pick = picked if isinstance(picked, TrackPick) else TrackPick(picked or ())
    names = {index: name for index, name, _enabled in tracks}
    roles = {index: pick.role_of(index, names.get(index, "")) for index in pick}
    return TrackPick(list(pick), pick.speakers, roles)


def speech_tracks(tracks, picked) -> list[tuple[int, str, str]]:
    """``[(track index, role, name)]`` of the chosen tracks with speech, in track order: the person's name for
    one person, the track's name (or "Audio N") for several people."""
    pick = complete(tracks, picked)
    names = {index: name for index, name, _enabled in tracks}
    out = []
    for index in sorted(pick):
        role = pick.roles[index]
        if role == ROLE_NONE:
            continue
        name = pick.speakers[index] if role == ROLE_PERSON else clean_name(names.get(index)) or "Audio %d" % index
        out.append((index, role, name))
    return out


def default_selection(config: dict, timeline_name: str, tracks) -> TrackPick:
    """What starts ticked: the tracks picked last time for this timeline (those that still exist), else
    the tracks currently enabled, else all of them; the speaker names given to them last time; and for
    every track, the role it had last time, else ``default_role``."""
    existing = [index for index, _name, _enabled in tracks]
    remembered = config_mod.remembered_audio_tracks(config, timeline_name) or []
    chosen = [index for index in remembered if index in existing]
    if not chosen:
        chosen = [index for index, _name, enabled in tracks if enabled]
    speakers = config_mod.remembered_track_speakers(config, timeline_name)
    roles = config_mod.remembered_track_roles(config, timeline_name)
    pick = TrackPick(chosen or existing, speakers)
    for index, name, _enabled in tracks:
        role = roles.get(index) or default_role(name, speakers.get(index, ""))
        pick.roles[index] = ROLE_GROUP if role == ROLE_PERSON and not pick.speakers.get(index) else role
    return pick


def initial_role(preselected, index: int, track_name: str) -> str:
    """The role a dialog row starts with."""
    role = roles_of(preselected).get(index)
    return role if role in ROLES else default_role(track_name, speakers_of(preselected).get(index, ""))


def track_label(index: int, name: str, enabled: bool = True) -> str:
    label = "%d  %s" % (index, name) if name else "Audio %d" % index
    return label if enabled else label + "  (disabled now)"


def chosen_indices(tracks, ticked) -> list[int]:
    """The track indices whose ``ticked(index)`` is true, in track order."""
    return [index for index, _name, _enabled in tracks if ticked(index)]


def chosen_pick(tracks, ticked, speaker, role=None) -> TrackPick:
    """The ticked tracks (``ticked(index)``) with the speaker name typed for each (``speaker(index)``) and
    the role chosen for each (``role(index)``; None: ``default_role``)."""
    chosen = chosen_indices(tracks, ticked)
    roles = {index: role(index) for index in chosen} if role is not None else {}
    return TrackPick(chosen, {index: speaker(index) or "" for index in chosen}, roles)


def problem(tracks, pick) -> str | None:
    """Why the dialog can't accept ``pick`` (shown in it), or None."""
    if not pick:
        return NONE_PICKED
    for index in pick:
        if pick.roles.get(index) == ROLE_PERSON and not pick.speakers.get(index):
            return NAME_MISSING % index
    if not speech_tracks(tracks, pick):
        return NO_SPEECH
    return None


def role_after_typing(role: str, typed: str) -> str:
    """Typing a name into a "Several people" row makes it "One person" (the default for a named track)."""
    return ROLE_PERSON if role == ROLE_GROUP and clean_name(typed) else role


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
        self.role_vars = {}
        speakers = speakers_of(preselected)
        rows = ttk.Frame(frame)
        rows.pack(fill="x")
        rows.columnconfigure(2, weight=1)
        ttk.Label(rows, text=ROLE_HEADING, foreground="#666").grid(row=0, column=1, sticky="w", padx=(12, 0))
        ttk.Label(rows, text=SPEAKER_PLACEHOLDER, foreground="#666").grid(row=0, column=2, sticky="w", padx=(12, 0))
        labels = [ROLE_LABELS[role] for role in ROLES]
        for row, (index, name, enabled) in enumerate(self.tracks, start=1):
            var = tk.BooleanVar(value=index in preselected)
            self.vars[index] = var
            ttk.Checkbutton(rows, text=track_label(index, name, enabled), variable=var).grid(
                row=row, column=0, sticky="w"
            )
            role = tk.StringVar(value=ROLE_LABELS[initial_role(preselected, index, name)])
            self.role_vars[index] = role
            ttk.Combobox(rows, textvariable=role, values=labels, state="readonly", width=14).grid(
                row=row, column=1, sticky="w", padx=(12, 0)
            )
            speaker = tk.StringVar(value=speakers.get(index, ""))
            self.speaker_vars[index] = speaker
            speaker.trace_add("write", lambda *_a, index=index: self._typed(index))
            ttk.Entry(rows, textvariable=speaker, width=20).grid(row=row, column=2, sticky="ew", padx=(12, 0))
        self.status_var = tk.StringVar(value="")
        ttk.Label(frame, textvariable=self.status_var, foreground="#b00").pack(anchor="w", pady=(6, 0))
        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(8, 0))
        ttk.Button(buttons, text="Cancel", command=self._cancel).pack(side="right", padx=(8, 0))
        ttk.Button(buttons, text="OK", command=self._ok).pack(side="right")
        self.window.bind("<Return>", lambda _e: self._ok())
        self.window.bind("<Escape>", lambda _e: self._cancel())
        self.window.update_idletasks()

    def role(self, index) -> str:
        label = self.role_vars[index].get()
        return next((role for role in ROLES if ROLE_LABELS[role] == label), ROLE_GROUP)

    def _typed(self, index):
        role = role_after_typing(self.role(index), self.speaker_vars[index].get())
        self.role_vars[index].set(ROLE_LABELS[role])

    def _ok(self):
        chosen = chosen_pick(
            self.tracks,
            lambda index: bool(self.vars[index].get()),
            lambda index: self.speaker_vars[index].get(),
            self.role,
        )
        message = problem(self.tracks, chosen)
        if message:
            self.status_var.set(message)
            return
        self.result = complete(self.tracks, chosen)
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
    """A modal checklist over ``root`` (``preselected`` may be a TrackPick, whose roles and speaker names
    fill the rows); the ticked tracks with their roles and speaker names, or None if cancelled."""
    return _TkTrackDialog(root, tracks, preselected).run()


# ---------------------------------------------------------------------------
# Fusion UIManager


def pick_audio_tracks_uimanager(fusion, ui, dispatcher, tracks, preselected) -> TrackPick | None:
    """The same checklist as a UIManager window (``ui`` = ``fusion.UIManager``, ``dispatcher`` = the
    ``UIDispatcher``); the ticked tracks with their roles and speaker names, or None if cancelled or closed."""
    speakers = speakers_of(preselected)
    start_roles = {index: initial_role(preselected, index, name) for index, name, _enabled in tracks}
    ticked = set(preselected)
    rows = [
        ui.HGroup(
            {"Weight": 0},
            [
                ui.CheckBox(
                    {
                        "ID": "Track_%d" % index,
                        "Text": track_label(index, name, enabled),
                        "Checked": index in ticked,
                        "Weight": 1,
                    }
                ),
                ui.ComboBox({"ID": "Role_%d" % index, "Weight": 0}),
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
            "Geometry": [250, 250, 780, 300 + 36 * len(tracks)],
        },
        [
            ui.VGroup(
                {"Spacing": 6},
                [
                    ui.Label({"Text": HEADING, "Weight": 0}),
                    # Wrapped labels get no height of their own in UIManager: reserve two lines each.
                    ui.Label({"Text": NOTE, "WordWrap": True, "Weight": 0, "MinimumSize": [0, NOTE_HEIGHT]}),
                    ui.Label({"Text": SPEAKER_NOTE, "WordWrap": True, "Weight": 0, "MinimumSize": [0, NOTE_HEIGHT]}),
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
    for index, _name, _enabled in tracks:
        box = items["Role_%d" % index]
        for role in ROLES:
            box.AddItem(ROLE_LABELS[role])
        box.CurrentIndex = ROLES.index(start_roles[index])

    def role(index):
        try:
            return ROLES[int(items["Role_%d" % index].CurrentIndex)]
        except (TypeError, ValueError, IndexError):
            return ROLE_GROUP

    def on_typed(index):
        def handler(_event=None):
            items["Role_%d" % index].CurrentIndex = ROLES.index(
                role_after_typing(role(index), str(items["Speaker_%d" % index].Text or ""))
            )

        return handler

    def on_ok(_event=None):
        chosen = chosen_pick(
            tracks,
            lambda index: bool(items["Track_%d" % index].Checked),
            lambda index: str(items["Speaker_%d" % index].Text or ""),
            role,
        )
        message = problem(tracks, chosen)
        if message:
            items["TracksStatus"].Text = message
            return
        state["result"] = complete(tracks, chosen)
        dispatcher.ExitLoop()

    def on_cancel(_event=None):
        dispatcher.ExitLoop()

    for index, _name, _enabled in tracks:
        window.On["Speaker_%d" % index].TextChanged = on_typed(index)
    window.On.Ok.Clicked = on_ok
    window.On.Cancel.Clicked = on_cancel
    window.On[WINDOW_ID_TRACKS].Close = on_cancel
    window.Show()
    dispatcher.RunLoop()
    window.Hide()
    return state["result"]
