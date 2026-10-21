"""A scripted UI backend: records everything, answers the review dialog from a callable."""

from __future__ import annotations

from better_captions_plugin.ui import Backend, ProgressView, ReviewRequest, ReviewResult, speaker_rows


class FakeProgress(ProgressView):
    def __init__(self, title, cancel_after=None, cancel_when=None):
        self.title = title
        self.statuses = []
        self.progress = []
        self.lines = []
        self.pumps = 0
        self.closed = False
        self._cancel_after = cancel_after
        self._cancel_when = cancel_when
        self._cancelled = False

    def set_status(self, text):
        self.statuses.append(text)

    def set_progress(self, pct):
        self.progress.append(pct)

    def log(self, line):
        self.lines.append(line)

    def pump(self):
        self.pumps += 1
        if self._cancel_when is not None and self._cancel_when(self):
            self._cancelled = True
        if self._cancel_after is not None and self.pumps >= self._cancel_after:
            self._cancelled = True

    @property
    def cancelled(self):
        return self._cancelled

    def close(self):
        self.closed = True


class FakeBackend(Backend):
    name = "fake"

    def __init__(self, answer=None, cancel_after=None, cancel_when=None):
        """``answer``: ReviewResult, None (cancel) or a callable(ReviewRequest) -> ReviewResult|None."""
        self._answer = answer
        self.cancel_after = cancel_after
        self.cancel_when = cancel_when
        self.progress_views = []
        self.requests = []
        self.messages = []
        self.errors = []
        self.closed = False

    def progress(self, title):
        view = FakeProgress(title, cancel_after=self.cancel_after, cancel_when=self.cancel_when)
        self.progress_views.append(view)
        return view

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


def accept_all(names=None, subtitles=True, textplus=False, template_index=None):
    """An answer that keeps the suggested names (overridden by ``names``) and picks outputs."""

    def answer(request: ReviewRequest):
        result = ReviewResult(subtitles=subtitles, textplus=textplus)
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
