"""Fusion UIManager backend (Resolve Studio; UIManager is Studio-only since 19.1).

UIManager has no progress-bar widget, so the bar is a Label of block characters (plain text, so it
renders the same in every Resolve version) above the percentage label and a log box; the event loop
is pumped with ``UIDispatcher.StepLoop()`` while the engine runs.
"""

from __future__ import annotations

from better_captions_plugin.ui import (
    HIGHLIGHT_COLOR_LABEL,
    HIGHLIGHT_LABEL,
    NAMING_CHOICES,
    NAMING_LABEL,
    PAUSE_LABEL,
    PAUSED_PREFIX,
    RESUME_LABEL,
    Backend,
    ProgressView,
    ReviewRequest,
    ReviewResult,
    checked_highlight_color,
    naming_index,
    review_scroll,
    speaker_rows,
)
from better_captions_plugin.ui.speaker_editor import EditorController, cue_index

TITLE = "Better Captions"
WINDOW_ID_PROGRESS = "BetterCaptionsProgress"
WINDOW_ID_REVIEW = "BetterCaptionsReview"
WINDOW_ID_EDITOR = "BetterCaptionsSpeakers"
WINDOW_ID_MESSAGE = "BetterCaptionsMessage"


BAR_WIDTH = 40
BAR_FILLED = "█"
BAR_EMPTY = "░"


def progress_bar_text(pct, width: int = BAR_WIDTH) -> str:
    """A block-character bar for ``pct`` (0-100, clamped); an empty bar for None or an unparseable value."""
    try:
        filled = int(round(width * max(0.0, min(100.0, float(pct))) / 100.0))
    except (TypeError, ValueError):
        filled = 0
    return BAR_FILLED * filled + BAR_EMPTY * (width - filled)


class UIManagerProgress(ProgressView):
    def __init__(self, ui, dispatcher, title: str):
        self.ui = ui
        self.disp = dispatcher
        self._cancelled = False
        self._paused = False
        self._status_text = "Starting…"
        self._lines: list[str] = []
        self.window = dispatcher.AddWindow(
            {"ID": WINDOW_ID_PROGRESS, "WindowTitle": title, "Geometry": [200, 200, 560, 380]},
            [
                ui.VGroup(
                    {"Spacing": 6},
                    [
                        ui.Label({"ID": "Status", "Text": self._status_text, "Weight": 0}),
                        ui.Label({"ID": "Steps", "Text": "", "Weight": 0}),
                        ui.Label({"ID": "Bar", "Text": progress_bar_text(None), "Weight": 0}),
                        ui.Label({"ID": "Percent", "Text": "", "Weight": 0}),
                        ui.TextEdit({"ID": "Log", "ReadOnly": True, "Weight": 1}),
                        ui.HGroup(
                            {"Weight": 0},
                            [
                                ui.HGap(0, 1),
                                ui.Button({"ID": "Pause", "Text": PAUSE_LABEL, "Weight": 0}),
                                ui.Button({"ID": "Cancel", "Text": "Cancel", "Weight": 0}),
                            ],
                        ),
                    ],
                )
            ],
        )
        self.items = self.window.GetItems()
        self.window.On[WINDOW_ID_PROGRESS].Close = self._on_cancel
        self.window.On.Cancel.Clicked = self._on_cancel
        self.window.On.Pause.Clicked = self._on_pause
        self.window.Show()
        self.pump()

    def _on_cancel(self, _event=None):
        self._cancelled = True
        self._paused = False  # a paused engine has to run again to be stopped
        self._status_text = "Cancelling…"
        self._show_status()

    def _on_pause(self, _event=None):
        if self._cancelled:
            return
        self._paused = not self._paused
        self.items["Pause"].Text = RESUME_LABEL if self._paused else PAUSE_LABEL
        self._show_status()

    def _show_status(self):
        self.items["Status"].Text = (PAUSED_PREFIX if self._paused else "") + self._status_text

    def _show_steps(self):
        self.items["Steps"].Text = "\n".join(self.step_lines())
        self.pump()

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    @property
    def paused(self) -> bool:
        return self._paused

    def set_status(self, text: str):
        self._status_text = str(text)
        self._show_status()
        self.pump()

    def set_progress(self, pct):
        self.items["Percent"].Text = "" if pct is None else "%d%%" % int(max(0.0, min(100.0, float(pct))))
        try:
            self.items["Bar"].Text = progress_bar_text(pct)
        except Exception:
            pass  # the bar is decoration: the percentage text still shows the progress
        self.pump()

    def log(self, line: str):
        self._lines.append(str(line).rstrip("\n"))
        self._lines = self._lines[-200:]
        self.items["Log"].PlainText = "\n".join(self._lines)
        self.pump()

    def pump(self):
        try:
            self.disp.StepLoop()
        except Exception:
            pass

    def close(self):
        try:
            self.window.Hide()
        except Exception:
            pass


class UIManagerBackend(Backend):
    name = "uimanager"

    def __init__(self, fusion, bmd):
        if fusion is None or bmd is None:
            raise RuntimeError("fusion/bmd objects are required")
        self.ui = fusion.UIManager
        if not self.ui:
            raise RuntimeError("fusion.UIManager is not available")
        self.disp = bmd.UIDispatcher(self.ui)

    def progress(self, title: str) -> ProgressView:
        return UIManagerProgress(self.ui, self.disp, title)

    def pick_audio_tracks(self, tracks, preselected) -> list[int] | None:
        from better_captions_plugin import track_picker

        return track_picker.pick_audio_tracks_uimanager(None, self.ui, self.disp, tracks, preselected)

    def pick_model(self, models, current: str) -> str | None:
        from better_captions_plugin import model_picker

        return model_picker.pick_model_uimanager(None, self.ui, self.disp, models, current)

    def message(self, title: str, text: str):
        ui, disp = self.ui, self.disp
        window = disp.AddWindow(
            {"ID": WINDOW_ID_MESSAGE, "WindowTitle": title, "Geometry": [250, 250, 520, 260]},
            [
                ui.VGroup(
                    {"Spacing": 8},
                    [
                        ui.TextEdit({"ID": "Body", "ReadOnly": True, "PlainText": str(text), "Weight": 1}),
                        ui.HGroup({"Weight": 0}, [ui.HGap(0, 1), ui.Button({"ID": "Ok", "Text": "OK", "Weight": 0})]),
                    ],
                )
            ],
        )

        def close(_event=None):
            disp.ExitLoop()

        window.On.Ok.Clicked = close
        window.On[WINDOW_ID_MESSAGE].Close = close
        window.Show()
        disp.RunLoop()
        window.Hide()

    def review(self, request: ReviewRequest) -> ReviewResult | None:
        """The review window; "Fix speakers…" closes it, runs the editor and reopens it rebuilt."""
        speakers = list(request.speakers)
        template_choice: dict[str, int] = {}
        checked = {
            "Subtitles": bool(request.default_subtitles and request.allow_subtitles),
            "TextPlus": bool(request.default_textplus and request.templates),
            "Highlight": bool(request.highlight_words),
        }
        color = request.highlight_color
        naming = naming_index(request.naming_provider)
        # UIManager has no scroll container, so more than VISIBLE_ROWS speakers are shown a page at a time
        # (the window is rebuilt per page, like for "Fix speakers…"); edits are kept across pages in `names`.
        names: dict[str, str] = {}
        page = 0
        while True:
            page = review_scroll.clamp_page(page, len(speakers))
            start, stop = review_scroll.page_bounds(page, len(speakers))
            action, items = self._review_once(request, speakers, template_choice, checked, color, naming, names, page)
            if action in ("fix", "prev", "next", "ok"):
                # Edits on the page just shown: the other pages' edits are already in names/template_choice.
                for speaker in speakers[start:stop]:
                    names[speaker.id] = str(items["Name_" + speaker.id].Text)
                    if request.templates:
                        template_choice[speaker.id] = int(items["Template_" + speaker.id].CurrentIndex)
            if action in ("prev", "next"):
                checked = {key: bool(items[key].Checked) for key in checked}
                color = str(items["HighlightColor"].Text)
                naming = self._naming_choice(items)
                page += -1 if action == "prev" else 1
                continue
            if action == "fix":
                request.editor.record_names({s.id: names.get(s.id, s.name) for s in speakers})
                checked = {key: bool(items[key].Checked) for key in checked}
                color = str(items["HighlightColor"].Text)
                naming = self._naming_choice(items)
                self.edit_speakers(EditorController(request.editor, request.time_label))
                speakers = speaker_rows(request.editor)
                names.clear()  # the editor now holds the typed names; rows come back from it
                page = 0
                continue
            if action != "ok":
                return None
            result = ReviewResult(
                subtitles=bool(items["Subtitles"].Checked),
                textplus=bool(items["TextPlus"].Checked),
                highlight_words=bool(items["Highlight"].Checked),
                highlight_color=checked_highlight_color(str(items["HighlightColor"].Text)) or request.highlight_color,
                naming_provider=NAMING_CHOICES[self._naming_choice(items)][0],
            )
            for index, speaker in enumerate(speakers):
                result.names[speaker.id] = names.get(speaker.id, speaker.name).strip() or speaker.id
                if request.templates:
                    result.template_index[speaker.id] = template_choice.get(
                        speaker.id, min(index, len(request.templates) - 1)
                    )
            return result

    @staticmethod
    def _naming_choice(items) -> int:
        index = int(items["Naming"].CurrentIndex)
        return index if 0 <= index < len(NAMING_CHOICES) else 0

    def _review_once(
        self, request: ReviewRequest, speakers, template_choice, checked, color, naming=0, names=None, page=0
    ):
        """Show one page of the review window until OK, Cancel, "Fix speakers…" or Prev/Next.

        Returns (action, items); action is ok, cancel, fix, prev or next, and ``items`` holds the widgets of
        the speakers on this page only.
        """
        ui, disp = self.ui, self.disp
        names = names or {}
        template_labels = list(request.templates)
        total = len(speakers)
        start, stop = review_scroll.page_bounds(page, total)
        pages = review_scroll.page_count(total)
        shown = speakers[start:stop]
        rows = []
        for speaker in shown:
            rows.append(
                ui.HGroup(
                    {"Spacing": 8, "Weight": 0},
                    [
                        ui.Label({"Text": "%s (%d cues)" % (speaker.id, speaker.cue_count), "Weight": 0.25}),
                        ui.LineEdit(
                            {"ID": "Name_" + speaker.id, "Text": names.get(speaker.id, speaker.name), "Weight": 0.3}
                        ),
                        ui.ComboBox({"ID": "Template_" + speaker.id, "Weight": 0.45}),
                    ],
                )
            )
            rows.append(ui.Label({"Text": "    says, e.g.: " + _shorten(speaker.sample, 70), "Weight": 0}))
        header = []
        if request.timeline_name:
            text = "Timeline: %s" % request.timeline_name
            if request.range_label:
                text += "    Range: %s" % request.range_label
            header.append(ui.Label({"Text": text, "Weight": 0}))
        if request.note:
            header.append(ui.Label({"Text": request.note, "Weight": 0}))
        buttons = [
            ui.HGap(0, 1),
            ui.Button({"ID": "Cancel", "Text": "Cancel", "Weight": 0}),
            ui.Button({"ID": "Ok", "Text": "Create tracks", "Weight": 0}),
        ]
        if request.editor is not None:
            buttons.insert(0, ui.Button({"ID": "FixSpeakers", "Text": "Fix speakers…", "Weight": 0}))
        pager = []
        if pages > 1:
            pager = [
                ui.HGroup(
                    {"Spacing": 8, "Weight": 0},
                    [
                        ui.Button({"ID": "PrevPage", "Text": "◀ Prev", "Enabled": page > 0, "Weight": 0}),
                        ui.Label({"Text": review_scroll.page_label(page, total), "Weight": 1}),
                        ui.Button({"ID": "NextPage", "Text": "Next ▶", "Enabled": page < pages - 1, "Weight": 0}),
                    ],
                )
            ]
        window = disp.AddWindow(
            {
                "ID": WINDOW_ID_REVIEW,
                "WindowTitle": "%s – review speakers" % TITLE,
                "Geometry": [200, 200, 760, 240 + 48 * len(shown) + (40 if pager else 0)],
            },
            [
                ui.VGroup(
                    {"Spacing": 6},
                    [
                        *header,
                        ui.HGroup(
                            {"Spacing": 8, "Weight": 0},
                            [
                                ui.Label({"Text": "Speaker", "Weight": 0.25}),
                                ui.Label({"Text": "Name (track name)", "Weight": 0.3}),
                                ui.Label({"Text": "Text+ template", "Weight": 0.45}),
                            ],
                        ),
                        *rows,
                        *pager,
                        ui.CheckBox(
                            {
                                "ID": "Subtitles",
                                "Text": "Subtitle tracks: one subtitle track per speaker, named after the speaker",
                                "Checked": checked["Subtitles"],
                                "Enabled": bool(request.allow_subtitles),
                                "Weight": 0,
                            }
                        ),
                        ui.CheckBox(
                            {
                                "ID": "TextPlus",
                                "Text": "Text+ captions: one video track per speaker using the chosen template",
                                "Checked": checked["TextPlus"],
                                "Enabled": bool(template_labels),
                                "Weight": 0,
                            }
                        ),
                        ui.HGroup(
                            {"Spacing": 8, "Weight": 0},
                            [
                                ui.HGap(16, 0),
                                ui.CheckBox(
                                    {
                                        "ID": "Highlight",
                                        "Text": HIGHLIGHT_LABEL,
                                        "Checked": checked["Highlight"],
                                        "Enabled": bool(template_labels),
                                        "Weight": 1,
                                    }
                                ),
                                ui.Label({"Text": HIGHLIGHT_COLOR_LABEL, "Weight": 0}),
                                ui.LineEdit(
                                    {
                                        "ID": "HighlightColor",
                                        "Text": str(color),
                                        "Enabled": bool(template_labels),
                                        "Weight": 0,
                                    }
                                ),
                            ],
                        ),
                        ui.Label({"Text": "New tracks are added; existing tracks are never changed.", "Weight": 0}),
                        ui.HGroup(
                            {"Spacing": 8, "Weight": 0},
                            [
                                ui.Label({"Text": NAMING_LABEL, "Weight": 0}),
                                ui.ComboBox({"ID": "Naming", "Weight": 1}),
                            ],
                        ),
                        ui.Label({"ID": "ReviewStatus", "Text": "", "Weight": 0}),
                        ui.HGroup({"Weight": 0}, buttons),
                    ],
                )
            ],
        )
        items = window.GetItems()
        for index, speaker in enumerate(shown, start=start):
            box = items["Template_" + speaker.id]
            for label in template_labels or ["(no Text+ templates in the media pool)"]:
                box.AddItem(label)
            default = min(index, len(template_labels) - 1) if template_labels else 0
            box.CurrentIndex = template_choice.get(speaker.id, default)
        for _value, label in NAMING_CHOICES:
            items["Naming"].AddItem(label)
        items["Naming"].CurrentIndex = naming
        state = {"action": "cancel"}

        def finish(action):
            state["action"] = action
            disp.ExitLoop()

        def on_ok(_event=None):
            if not items["Subtitles"].Checked and not items["TextPlus"].Checked:
                items["ReviewStatus"].Text = "Pick at least one output: subtitle tracks and/or Text+ captions."
                return
            if (
                items["Highlight"].Checked
                and items["TextPlus"].Checked
                and checked_highlight_color(str(items["HighlightColor"].Text)) is None
            ):
                items["ReviewStatus"].Text = "The highlight colour must look like #FFD400."
                return
            finish("ok")

        window.On.Ok.Clicked = on_ok
        window.On.Cancel.Clicked = lambda _event=None: finish("cancel")
        window.On.FixSpeakers.Clicked = lambda _event=None: finish("fix")
        window.On.PrevPage.Clicked = lambda _event=None: finish("prev")
        window.On.NextPage.Clicked = lambda _event=None: finish("next")
        window.On[WINDOW_ID_REVIEW].Close = lambda _event=None: finish("cancel")
        window.Show()
        disp.RunLoop()
        window.Hide()
        return state["action"], items

    def edit_speakers(self, controller: EditorController):
        """The "Fix speakers" window: speaker list (rename, merge into…), cue list (filter, move), undo."""
        ui, disp = self.ui, self.disp
        window = disp.AddWindow(
            {"ID": WINDOW_ID_EDITOR, "WindowTitle": "%s – fix speakers" % TITLE, "Geometry": [160, 160, 940, 540]},
            [
                ui.VGroup(
                    {"Spacing": 6},
                    [
                        ui.HGroup(
                            {"Spacing": 10, "Weight": 1},
                            [
                                ui.VGroup(
                                    {"Spacing": 6, "Weight": 0.35},
                                    [
                                        ui.Label({"Text": "Speakers", "Weight": 0}),
                                        ui.Tree({"ID": "SpeakerTree", "SortingEnabled": False, "Weight": 1}),
                                        ui.HGroup(
                                            {"Weight": 0},
                                            [
                                                ui.Label({"Text": "Name:", "Weight": 0}),
                                                ui.LineEdit({"ID": "SpeakerName", "Weight": 1}),
                                                ui.Button({"ID": "Rename", "Text": "Rename", "Weight": 0}),
                                            ],
                                        ),
                                        ui.HGroup(
                                            {"Weight": 0},
                                            [
                                                ui.Label({"Text": "Merge into:", "Weight": 0}),
                                                ui.ComboBox({"ID": "MergeTarget", "Weight": 1}),
                                                ui.Button({"ID": "Merge", "Text": "Merge", "Weight": 0}),
                                            ],
                                        ),
                                        ui.Label(
                                            {
                                                "Text": (
                                                    "Merge moves all of the selected speaker's cues;\n"
                                                    "the speaker merged into keeps its name."
                                                ),
                                                "WordWrap": True,
                                                "Weight": 0,
                                            }
                                        ),
                                    ],
                                ),
                                ui.VGroup(
                                    {"Spacing": 6, "Weight": 0.65},
                                    [
                                        ui.HGroup(
                                            {"Weight": 0},
                                            [
                                                ui.Label({"Text": "Cues    Show:", "Weight": 0}),
                                                ui.ComboBox({"ID": "Filter", "Weight": 1}),
                                            ],
                                        ),
                                        ui.Tree(
                                            {
                                                "ID": "CueTree",
                                                "SortingEnabled": False,
                                                "SelectionMode": "ExtendedSelection",
                                                "Weight": 1,
                                            }
                                        ),
                                        ui.HGroup(
                                            {"Weight": 0},
                                            [
                                                ui.Label({"Text": "Move selected to:", "Weight": 0}),
                                                ui.ComboBox({"ID": "MoveTarget", "Weight": 1}),
                                                ui.Button({"ID": "Move", "Text": "Move", "Weight": 0}),
                                            ],
                                        ),
                                    ],
                                ),
                            ],
                        ),
                        ui.HGroup(
                            {"Weight": 0},
                            [
                                ui.Button({"ID": "Undo", "Text": "Undo", "Weight": 0}),
                                ui.Label({"ID": "Status", "Text": "", "Weight": 1}),
                                ui.Button({"ID": "Done", "Text": "Done", "Weight": 0}),
                            ],
                        ),
                    ],
                )
            ],
        )
        items = window.GetItems()
        _set_header(items["SpeakerTree"], ["Speaker", "Name", "Cues"], [110, 140, 50])
        _set_header(items["CueTree"], ["#", "Time", "Speaker", "Text"], [50, 100, 120, 380])
        state = {"speaker": None, "refreshing": False, "choices": {}}

        def choice(box_id):
            ids = state["choices"].get(box_id, [])
            index = int(items[box_id].CurrentIndex)
            return ids[index] if 0 <= index < len(ids) else None

        def set_choices(box_id, choices, keep=None):
            box = items[box_id]
            ids = [value for _label, value in choices]
            state["choices"][box_id] = ids
            box.Clear()
            for label, _value in choices:
                box.AddItem(label)
            box.CurrentIndex = ids.index(keep) if keep in ids else 0

        def refresh_cues():
            tree = items["CueTree"]
            tree.Clear()
            for row in controller.cue_rows(choice("Filter")):
                _add_row(tree, row)

        def refresh_speaker_tools():
            speaker_id = state["speaker"]
            items["SpeakerName"].Text = controller.editor.display_name(speaker_id) if speaker_id else ""
            set_choices("MergeTarget", controller.merge_choices(speaker_id), keep=choice("MergeTarget"))

        def refresh():
            state["refreshing"] = True
            try:
                if state["speaker"] not in controller.speaker_ids():
                    state["speaker"] = None
                tree = items["SpeakerTree"]
                tree.Clear()
                for row in controller.speaker_rows():
                    _add_row(tree, row)
                set_choices("Filter", controller.filter_choices(), keep=choice("Filter"))
                set_choices("MoveTarget", controller.move_choices(), keep=choice("MoveTarget"))
                refresh_speaker_tools()
                refresh_cues()
                items["Undo"].Enabled = controller.can_undo
                items["Status"].Text = controller.status
            finally:
                state["refreshing"] = False

        def on_speaker_clicked(event=None):
            item = (event or {}).get("item") if isinstance(event, dict) else None
            speaker_id = _item_text(item) or _item_text(_current_item(items["SpeakerTree"]))
            if speaker_id:
                state["speaker"] = speaker_id
                refresh_speaker_tools()

        def on_filter_changed(_event=None):
            if not state["refreshing"]:
                refresh_cues()

        def act(action):
            def handler(_event=None):
                action()
                refresh()

            return handler

        window.On.SpeakerTree.ItemClicked = on_speaker_clicked
        window.On.Filter.CurrentIndexChanged = on_filter_changed
        window.On.Rename.Clicked = act(lambda: controller.rename(state["speaker"], str(items["SpeakerName"].Text)))
        window.On.Merge.Clicked = act(lambda: controller.merge(state["speaker"], choice("MergeTarget")))
        window.On.Move.Clicked = act(
            lambda: controller.move(
                [cue_index(_item_text(item)) for item in _selected_items(items["CueTree"])], choice("MoveTarget")
            )
        )
        window.On.Undo.Clicked = act(controller.undo)
        window.On.Done.Clicked = lambda _event=None: disp.ExitLoop()
        window.On[WINDOW_ID_EDITOR].Close = lambda _event=None: disp.ExitLoop()
        refresh()
        window.Show()
        disp.RunLoop()
        window.Hide()


def _set_header(tree, labels, widths):
    tree.ColumnCount = len(labels)
    header = tree.NewItem()
    for column, label in enumerate(labels):
        header.Text[column] = label
    tree.SetHeaderItem(header)
    for column, width in enumerate(widths):
        try:
            tree.ColumnWidth[column] = width
        except Exception:
            pass


def _add_row(tree, values):
    item = tree.NewItem()
    for column, value in enumerate(values):
        item.Text[column] = str(value)
    tree.AddTopLevelItem(item)


def _item_text(item, column: int = 0) -> str | None:
    if not item:
        return None
    try:
        return str(item.Text[column]) or None
    except Exception:
        return None


def _current_item(tree):
    try:
        return tree.CurrentItem()
    except Exception:
        return None


def _selected_items(tree) -> list:
    """The tree's selected rows (a dict keyed 1..n through the Fusion bridge), else its current row."""
    try:
        selected = tree.SelectedItems()
    except Exception:
        selected = None
    if isinstance(selected, dict):
        selected = [selected[key] for key in sorted(selected)]
    selected = [item for item in (selected or []) if item]
    if not selected:
        current = _current_item(tree)
        selected = [current] if current else []
    return selected


def _shorten(text: str, limit: int) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"
