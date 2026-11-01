"""A scripted UI backend: records everything, answers the review dialog from a callable."""

from __future__ import annotations

from better_captions_plugin import track_picker
from better_captions_plugin.ui import Backend, ProgressView, ReviewRequest, ReviewResult, speaker_rows


class FakeProgress(ProgressView):
    """``pause_when(view)`` presses Pause once, the first time it is true during a pump;
    ``resume_when(view)`` presses Resume when it is true during a pump while paused."""

    def __init__(self, title, cancel_after=None, cancel_when=None, pause_when=None, resume_when=None):
        self.title = title
        self.statuses = []
        self.progress = []
        self.lines = []
        self.pumps = 0
        self.closed = False
        self.step_log = []  # (step index, was paused) per set_step
        self.paused_pumps = 0
        self.resumes = 0
        self._cancel_after = cancel_after
        self._cancel_when = cancel_when
        self._pause_when = pause_when
        self._resume_when = resume_when
        self._pause_pressed = False
        self._paused = False
        self._cancelled = False

    def set_status(self, text):
        self.statuses.append(text)

    def set_progress(self, pct):
        self.progress.append(pct)

    def log(self, line):
        self.lines.append(line)

    def set_step(self, index):
        self.step_log.append((index, self._paused))
        super().set_step(index)
        self._maybe_pause()

    def _maybe_pause(self):
        idle = not (self._cancelled or self._paused or self._pause_pressed)
        if idle and self._pause_when is not None and self._pause_when(self):
            self._pause_pressed = True
            self._paused = True

    def pump(self):
        self.pumps += 1
        if self._cancel_when is not None and self._cancel_when(self):
            self._cancelled = True
        if self._cancel_after is not None and self.pumps >= self._cancel_after:
            self._cancelled = True
        if self._cancelled:
            self._paused = False
        elif self._paused:
            self.paused_pumps += 1
            if self._resume_when is not None and self._resume_when(self):
                self._paused = False
                self.resumes += 1
        else:
            self._maybe_pause()

    @property
    def cancelled(self):
        return self._cancelled

    @property
    def paused(self):
        return self._paused

    def close(self):
        self.closed = True


class FakeBackend(Backend):
    name = "fake"

    def __init__(
        self,
        answer=None,
        cancel_after=None,
        cancel_when=None,
        pause_when=None,
        resume_when=None,
        tracks_answer="default",
        model_answer="default",
        speakers_answer=None,
    ):
        """``answer``: ReviewResult, None (cancel) or a callable(ReviewRequest) -> ReviewResult|None.
        ``tracks_answer``: the audio track pick — "default" (the preselection, with its speaker names),
        None (cancel), a list or a TrackPick; ``speakers_answer`` ({track index: name}) names the speakers
        of a ``tracks_answer`` list.
        ``model_answer``: the model pick — "default" (the preselected model), None (cancel) or a model name."""
        self._answer = answer
        self._tracks_answer = tracks_answer
        self._speakers_answer = speakers_answer
        self._model_answer = model_answer
        self.model_requests = []  # (models, current) per pick_model
        self.track_requests = []
        self.track_speaker_requests = []  # the speaker names preselected, per pick_audio_tracks
        self.cancel_after = cancel_after
        self.cancel_when = cancel_when
        self.pause_when = pause_when
        self.resume_when = resume_when
        self.progress_views = []
        self.requests = []
        self.messages = []
        self.errors = []
        self.closed = False

    def progress(self, title):
        view = FakeProgress(
            title,
            cancel_after=self.cancel_after,
            cancel_when=self.cancel_when,
            pause_when=self.pause_when,
            resume_when=self.resume_when,
        )
        self.progress_views.append(view)
        return view

    def pick_audio_tracks(self, tracks, preselected):
        self.track_requests.append((list(tracks), list(preselected)))
        self.track_speaker_requests.append(track_picker.speakers_of(preselected))
        if self._tracks_answer == "default":
            return track_picker.TrackPick(preselected, track_picker.speakers_of(preselected))
        if self._tracks_answer is not None and self._speakers_answer is not None:
            return track_picker.TrackPick(self._tracks_answer, self._speakers_answer)
        return self._tracks_answer

    def pick_model(self, models, current):
        self.model_requests.append((list(models), current))
        if self._model_answer == "default":
            return current
        return self._model_answer

    def review(self, request: ReviewRequest):
        self.requests.append(request)
        if callable(self._answer):
            return self._answer(request)
        return self._answer

    def message(self, title, text):
        self.messages.append((title, text))

    def error(self, title, text):
        self.errors.append((title, text))

    def close(self):
        self.closed = True


def accept_all(
    names=None,
    subtitles=True,
    textplus=False,
    template_index=None,
    highlight_words=None,
    highlight_color=None,
    naming_provider=None,
):
    """An answer that keeps the suggested names (overridden by ``names``) and picks outputs;
    the highlight toggle and colour keep the dialog's defaults unless given; the naming
    provider is left alone unless given."""

    def answer(request: ReviewRequest):
        result = ReviewResult(
            subtitles=subtitles,
            textplus=textplus,
            highlight_words=request.highlight_words if highlight_words is None else highlight_words,
            highlight_color=highlight_color or request.highlight_color,
            naming_provider=naming_provider,
        )
        for row in request.speakers:
            result.names[row.id] = (names or {}).get(row.id, row.name)
            if request.templates:
                result.template_index[row.id] = (template_index or {}).get(row.id, 0)
        return result

    return answer


def edit_then_accept(edit, names=None, subtitles=True, textplus=False, template_index=None):
    """An answer that first runs ``edit(request.editor)`` (as the "Fix speakers" dialog would),
    then accepts the edited speakers like ``accept_all``."""

    def answer(request: ReviewRequest):
        edit(request.editor)
        request = ReviewRequest(speakers=speaker_rows(request.editor), templates=request.templates)
        return accept_all(names, subtitles, textplus, template_index)(request)

    return answer
