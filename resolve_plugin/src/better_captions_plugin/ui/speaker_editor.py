"""What the "Fix speakers" dialog shows and does, shared by the tkinter and UIManager backends.

The dialog has a speaker list (rename, "merge into…"), a cue list (time, speaker, text;
filterable by speaker) whose selected cues can be moved to another or a new speaker, and
undo. The backends only lay out widgets and call ``EditorController``; every action returns
whether it worked and leaves a one-line ``status`` for the dialog.
"""

from __future__ import annotations

from better_captions_plugin.speaker_edits import EditError, SpeakerEditor

NEW_SPEAKER = "__new_speaker__"
ALL_SPEAKERS_LABEL = "All speakers"
NEW_SPEAKER_LABEL = "New speaker"


def default_time_label(seconds: float) -> str:
    minutes, secs = divmod(max(0.0, float(seconds)), 60.0)
    return "%d:%04.1f" % (int(minutes), secs)


def cue_number(index: int) -> str:
    """The label of a cue in the list ("#1" for index 0); ``cue_index`` reads it back."""
    return "#%d" % (index + 1)


def cue_index(label) -> int | None:
    text = str(label or "").strip().lstrip("#")
    return int(text) - 1 if text.isdigit() and int(text) > 0 else None


class EditorController:
    def __init__(self, editor: SpeakerEditor, time_label=None):
        self.editor = editor
        self.time_label = time_label or default_time_label
        self.status = ""

    # what to show

    def speaker_label(self, speaker_id: str) -> str:
        name = self.editor.display_name(speaker_id)
        return speaker_id if name == speaker_id else "%s (%s)" % (name, speaker_id)

    def speaker_rows(self) -> list[tuple[str, str, str]]:
        """(id, name, cue count) per speaker."""
        return [(s.id, s.name, str(s.cue_count)) for s in self.editor.speakers()]

    def speaker_ids(self) -> list[str]:
        return [s.id for s in self.editor.speakers()]

    def filter_choices(self) -> list[tuple[str, str | None]]:
        """(label, speaker id or None for all) for the cue list filter."""
        return [(ALL_SPEAKERS_LABEL, None)] + [(self.speaker_label(sid), sid) for sid in self.speaker_ids()]

    def merge_choices(self, source: str | None) -> list[tuple[str, str]]:
        return [(self.speaker_label(sid), sid) for sid in self.speaker_ids() if sid != source]

    def move_choices(self) -> list[tuple[str, str]]:
        return [(self.speaker_label(sid), sid) for sid in self.speaker_ids()] + [(NEW_SPEAKER_LABEL, NEW_SPEAKER)]

    def cue_rows(self, speaker_id: str | None = None) -> list[tuple[str, str, str, str]]:
        """(number, time, speaker, text) per cue, in time order; ``cue_index(number)`` is the cue."""
        return [
            (
                cue_number(c.index),
                self.time_label(c.start),
                self.editor.display_name(c.speaker),
                " / ".join(c.text.splitlines()),
            )
            for c in self.editor.cues(speaker_id)
        ]

    @property
    def can_undo(self) -> bool:
        return self.editor.can_undo

    # actions

    def rename(self, speaker_id: str | None, name: str) -> bool:
        if not speaker_id:
            return self._fail("Pick a speaker to rename.")
        if not str(name or "").strip():
            return self._fail("Type a name first.")
        return self._do(
            lambda: self.editor.rename(speaker_id, name), "Renamed %s to %s." % (speaker_id, str(name).strip())
        )

    def merge(self, source: str | None, target: str | None) -> bool:
        if not source or not target:
            return self._fail("Pick a speaker, then the speaker to merge it into.")
        message = "Merged %s into %s." % (self.speaker_label(source), self.speaker_label(target))
        return self._do(lambda: self.editor.merge(source, target), message)

    def move(self, cue_indexes, target: str | None, new_name: str | None = None) -> bool:
        cue_indexes = [i for i in cue_indexes if i is not None]
        if not cue_indexes:
            return self._fail("Select one or more cues first.")
        if not target:
            return self._fail("Pick the speaker to move the cues to.")
        count = "%d cue%s" % (len(cue_indexes), "" if len(cue_indexes) == 1 else "s")
        if target == NEW_SPEAKER:
            created = []
            ok = self._do(lambda: created.append(self.editor.split(cue_indexes, new_name)), "")
            if ok:
                self.status = "Moved %s to new speaker %s." % (count, self.speaker_label(created[0]))
            return ok
        return self._do(
            lambda: self.editor.reassign(cue_indexes, target), "Moved %s to %s." % (count, self.speaker_label(target))
        )

    def undo(self) -> bool:
        op = self.editor.undo()
        if op is None:
            return self._fail("Nothing to undo.")
        self.status = "Undid %s." % op.get("op", "edit")
        return True

    def _do(self, action, message: str) -> bool:
        try:
            action()
        except EditError as exc:
            return self._fail(str(exc))
        self.status = message
        return True

    def _fail(self, message: str) -> bool:
        self.status = message
        return False
