"""Which of the engine's voices the editor confirmed, so they can go into the voice library (stdlib only).

The engine writes ``voices.json`` next to ``transcript.json``: a voice centroid per speaker it detected
(``SPEAKER_00``...). After the review dialog, each of those speakers whose cues ended up, almost all
(``MIN_SHARE`` of their duration), with one speaker who has a real name (typed, or a guess the editor
left as it was) is that person: ``app.remember_voices`` has the engine keep those voices
(``better-captions voices save``), so the next video names them by voice. Speaker fixes are followed: a
speaker merged into Sam is Sam's voice too; one whose cues were split between people is not saved.
"""

from __future__ import annotations

import json
import os
import re

VOICES_FILE = "voices.json"
# Share of a detected speaker's cue time that must end up with one named speaker for it to be their voice.
MIN_SHARE = 0.8
_PLACEHOLDER = re.compile(r"^(speaker|pinned)[_ ]?\d+$", re.IGNORECASE)


def voice_speakers(path: str) -> list[str]:
    """The speaker ids with a voice in a run's ``voices.json``; [] when it is missing or unreadable."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return []
    speakers = data.get("speakers") if isinstance(data, dict) else None
    return [str(sid) for sid in speakers] if isinstance(speakers, dict) else []


def voices_path(out_dir: str, done: dict | None = None) -> str:
    """The run's ``voices.json``: as the engine's ``done`` event names it, else next to ``transcript.json``."""
    named = (done or {}).get("voices")
    return str(named) if named else os.path.join(out_dir, VOICES_FILE)


def clean_name(name) -> str:
    return re.sub(r"\s+", " ", str(name or "")).strip()


def is_real_name(name, speaker_id: str) -> bool:
    """A person's name: not empty, not the speaker's id, not a placeholder like ``SPEAKER_03``."""
    name = clean_name(name)
    return bool(name) and name != speaker_id and not _PLACEHOLDER.match(name)


def _duration(cue: dict) -> float:
    try:
        return max(0.01, float(cue.get("end", 0.0)) - float(cue.get("start", 0.0)))
    except (TypeError, ValueError):
        return 0.01


def confirmed_names(original: dict, reviewed: dict, speaker_ids) -> dict[str, str]:
    """Detected speaker id -> the name the editor confirmed for that voice, for those in ``speaker_ids``.

    ``original`` is the engine's transcript dict, ``reviewed`` the same after the review (speaker fixes and
    names applied, ``SpeakerEditor.data``); edits never add or remove cues, so cue ``i`` is the same line in both.
    """
    before = original.get("cues") or []
    after = reviewed.get("cues") or []
    names_after = {str(s.get("id")): s.get("name") for s in reviewed.get("speakers") or [] if isinstance(s, dict)}
    out = {}
    for speaker_id in speaker_ids:
        weights: dict[str, float] = {}
        total = 0.0
        for index, cue in enumerate(before):
            if str(cue.get("speaker")) != speaker_id or index >= len(after):
                continue
            weight = _duration(cue)
            total += weight
            now = str(after[index].get("speaker"))
            weights[now] = weights.get(now, 0.0) + weight
        if not total:
            continue
        final_id, weight = max(weights.items(), key=lambda item: item[1])
        name = clean_name(names_after.get(final_id))
        if weight / total >= MIN_SHARE and is_real_name(name, final_id):
            out[speaker_id] = name
    return out
