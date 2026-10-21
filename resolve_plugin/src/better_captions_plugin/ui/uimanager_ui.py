"""Fusion UIManager backend (Resolve Studio; UIManager is Studio-only since 19.1).

UIManager has no progress-bar widget, so progress is a label plus a log box; the event loop
is pumped with ``UIDispatcher.StepLoop()`` while the engine runs.
"""

from __future__ import annotations

from better_captions_plugin.ui import Backend, ProgressView, ReviewRequest, ReviewResult

TITLE = "Better Captions"
WINDOW_ID_PROGRESS = "BetterCaptionsProgress"
WINDOW_ID_REVIEW = "BetterCaptionsReview"
WINDOW_ID_MESSAGE = "BetterCaptionsMessage"


class UIManagerProgress(ProgressView):
    def __init__(self, ui, dispatcher, title: str):
        self.ui = ui
        self.disp = dispatcher
        self._cancelled = False
        self._lines: "list[str]" = []
        self.window = dispatcher.AddWindow(
            {"ID": WINDOW_ID_PROGRESS, "WindowTitle": title, "Geometry": [200, 200, 560, 320]},
            [
                ui.VGroup(
                    {"Spacing": 6},
                    [
                        ui.Label({"ID": "Status", "Text": "Starting…", "Weight": 0}),
                        ui.Label({"ID": "Percent", "Text": "", "Weight": 0}),
                        ui.TextEdit({"ID": "Log", "ReadOnly": True, "Weight": 1}),
                        ui.HGroup({"Weight": 0}, [ui.HGap(0, 1), ui.Button({"ID": "Cancel", "Text": "Cancel", "Weight": 0})]),
                    ],
                )
            ],
        )
        self.items = self.window.GetItems()
        self.window.On[WINDOW_ID_PROGRESS].Close = self._on_cancel
        self.window.On.Cancel.Clicked = self._on_cancel
        self.window.Show()
        self.pump()

    def _on_cancel(self, _event=None):
        self._cancelled = True
        self.items["Status"].Text = "Cancelling…"

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def set_status(self, text: str):
        self.items["Status"].Text = str(text)
        self.pump()

    def set_progress(self, pct):
        self.items["Percent"].Text = "" if pct is None else "%d%%" % int(float(pct))
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

    def review(self, request: ReviewRequest) -> "ReviewResult | None":
        ui, disp = self.ui, self.disp
        template_labels = list(request.templates)
        rows = []
        for speaker in request.speakers:
            rows.append(
                ui.HGroup(
                    {"Spacing": 8, "Weight": 0},
                    [
                        ui.Label({"Text": "%s (%d cues)" % (speaker.id, speaker.cue_count), "Weight": 0.25}),
                        ui.LineEdit({"ID": "Name_" + speaker.id, "Text": speaker.name, "Weight": 0.3}),
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
        window = disp.AddWindow(
            {"ID": WINDOW_ID_REVIEW, "WindowTitle": "%s – review speakers" % TITLE, "Geometry": [200, 200, 720, 160 + 48 * len(request.speakers)]},
            [
                ui.VGroup(
                    {"Spacing": 6},
                    header
                    + [
                        ui.HGroup(
                            {"Spacing": 8, "Weight": 0},
                            [
                                ui.Label({"Text": "Speaker", "Weight": 0.25}),
                                ui.Label({"Text": "Name (track name)", "Weight": 0.3}),
                                ui.Label({"Text": "Text+ template", "Weight": 0.45}),
                            ],
                        )
                    ]
                    + rows
                    + [
                        ui.CheckBox({"ID": "Subtitles", "Text": "Subtitle tracks: one subtitle track per speaker, named after the speaker",
                                     "Checked": bool(request.default_subtitles and request.allow_subtitles),
                                     "Enabled": bool(request.allow_subtitles), "Weight": 0}),
                        ui.CheckBox({"ID": "TextPlus", "Text": "Text+ captions: one video track per speaker using the chosen template",
                                     "Checked": bool(request.default_textplus and template_labels),
                                     "Enabled": bool(template_labels), "Weight": 0}),
                        ui.Label({"Text": "New tracks are added; existing tracks are never changed.", "Weight": 0}),
                        ui.HGroup(
                            {"Weight": 0},
                            [ui.HGap(0, 1), ui.Button({"ID": "Cancel", "Text": "Cancel", "Weight": 0}), ui.Button({"ID": "Ok", "Text": "Create tracks", "Weight": 0})],
                        ),
                    ],
                )
            ],
        )
        items = window.GetItems()
        for index, speaker in enumerate(request.speakers):
            box = items["Template_" + speaker.id]
            for label in template_labels or ["(no Text+ templates in the media pool)"]:
                box.AddItem(label)
            box.CurrentIndex = min(index, len(template_labels) - 1) if template_labels else 0
        state = {"ok": False}

        def on_ok(_event=None):
            if not items["Subtitles"].Checked and not items["TextPlus"].Checked:
                return
            state["ok"] = True
            disp.ExitLoop()

        def on_cancel(_event=None):
            state["ok"] = False
            disp.ExitLoop()

        window.On.Ok.Clicked = on_ok
        window.On.Cancel.Clicked = on_cancel
        window.On[WINDOW_ID_REVIEW].Close = on_cancel
        window.Show()
        disp.RunLoop()
        window.Hide()
        if not state["ok"]:
            return None
        result = ReviewResult(subtitles=bool(items["Subtitles"].Checked), textplus=bool(items["TextPlus"].Checked))
        for speaker in request.speakers:
            result.names[speaker.id] = str(items["Name_" + speaker.id].Text).strip() or speaker.id
            if template_labels:
                result.template_index[speaker.id] = int(items["Template_" + speaker.id].CurrentIndex)
        return result


def _shorten(text: str, limit: int) -> str:
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"
