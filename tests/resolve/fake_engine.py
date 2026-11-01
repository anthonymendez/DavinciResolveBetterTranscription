"""A stand-in for `better-captions run` used as a real subprocess by the plugin tests.

Usage: python fake_engine.py run [AUDIO] --out DIR [--track ROLE:NAME=PATH ...] [--fixed-track NAME=PATH ...]
       [--fail] [--crash] [--slow] [--with-words] [--with-voices] [--with-confidence]
       [...ignored flags]
       python fake_engine.py voices save --from DIR --name SPEAKER=NAME ...  (see voices_save)

Emits the same JSON-line events as the engine and writes transcript.json plus one SRT per
speaker from the fixture below (the shape matches src/better_captions/schema.py, see
tests/test_pipeline.py). Also prints a non-JSON line to stdout and a warning to stderr, as
model downloads do.
"""

import json
import os
import sys
import time

TRANSCRIPT = {
    "language": "en",
    "duration": 12.0,
    "speakers": [
        {"id": "SPEAKER_00", "name": "Maria", "name_confidence": 0.8, "name_source": "heuristic"},
        {"id": "SPEAKER_01", "name": None, "name_confidence": 0.0, "name_source": None},
    ],
    "segments": [],
    "cues": [
        {"speaker": "SPEAKER_00", "start": 0.5, "end": 2.0, "lines": ["Hey Jordan,", "how are you?"]},
        {"speaker": "SPEAKER_01", "start": 2.1, "end": 3.6, "lines": ["Good, thanks Maria."]},
        {"speaker": "SPEAKER_00", "start": 4.0, "end": 6.5, "lines": ["Ready for the race?"]},
        {"speaker": "SPEAKER_01", "start": 6.4, "end": 8.0, "lines": ["Always."]},
    ],
    "meta": {"audio": "", "model": "fake"},
    "schema_version": 1,
}


# ``--with-confidence``: per-cue speaker_confidence (cue order). Cues 2 and 4 are uncertain
# (below the plugin's UNCERTAIN_BELOW of 0.6), cue 3's is unknown.
CONFIDENCES = (0.95, 0.42, None, 0.31)


def with_confidence(transcript, confidences=CONFIDENCES):
    """``transcript`` with ``speaker_confidence`` on each cue (left out where the value is None)."""
    cues = []
    for cue, confidence in zip(transcript["cues"], confidences, strict=False):
        cue = dict(cue)
        if confidence is not None:
            cue["speaker_confidence"] = confidence
        cues.append(cue)
    return dict(transcript, cues=cues)


def with_words(transcript):
    """``transcript`` with one segment per cue whose words share the cue's time evenly (``--with-words``)."""
    segments = []
    for cue in transcript["cues"]:
        tokens = " ".join(cue["lines"]).split()
        step = (cue["end"] - cue["start"]) / len(tokens)
        words = [
            {
                "text": " " + token,
                "start": round(cue["start"] + i * step, 3),
                "end": round(cue["start"] + (i + 0.8) * step, 3),
                "probability": 0.9,
                "speaker": cue["speaker"],
            }
            for i, token in enumerate(tokens)
        ]
        segments.append(
            {
                "speaker": cue["speaker"],
                "start": cue["start"],
                "end": cue["end"],
                "text": " ".join(tokens),
                "words": words,
            }
        )
    return dict(transcript, segments=segments)


def with_fixed_tracks(transcript, specs, meta_key="fixed_tracks"):
    """``--fixed-track NAME=PATH`` (or a ``--track person:NAME=PATH``), as the engine reports it: the
    first pinned speaker says what SPEAKER_01 said, under id PINNED_00, already named (name_source
    "user"). None (after an error event) when a track's audio is missing."""
    ids = {}
    tracks = []
    for spec in specs:
        name, _sep, path = spec.partition("=")
        if not os.path.isfile(path):
            emit({"event": "error", "message": "FileNotFoundError: %s" % path})
            return None
        sid = ids.setdefault(name, "PINNED_%02d" % len(ids))
        tracks.append({"id": sid, "name": name, "audio": path})
    if not ids:
        return transcript
    for cue in transcript["cues"]:
        if cue["speaker"] == "SPEAKER_01":
            cue["speaker"] = "PINNED_00"
    transcript["speakers"] = [s for s in transcript["speakers"] if s["id"] != "SPEAKER_01"] + [
        {"id": sid, "name": name, "name_confidence": 1.0, "name_source": "user"} for name, sid in ids.items()
    ]
    transcript["meta"][meta_key] = tracks
    return transcript


def with_tracks(transcript, specs):
    """``--track ROLE:NAME=PATH`` (each track transcribed alone): person tracks as ``with_fixed_tracks``;
    meta "tracks" lists every track. None (after an error event) when a track's audio is missing."""
    persons, tracks = [], []
    for spec in specs:
        left, _sep, path = spec.partition("=")
        role, _colon, name = left.partition(":")
        if not os.path.isfile(path):
            emit({"event": "error", "message": "FileNotFoundError: %s" % path})
            return None
        tracks.append({"role": role, "name": name, "audio": path})
        if role == "person":
            persons.append("%s=%s" % (name, path))
    transcript = with_fixed_tracks(transcript, persons, meta_key="persons")
    if transcript is not None:
        transcript["meta"]["tracks"] = tracks
    return transcript


def _ts(seconds):
    ms = int(round(seconds * 1000))
    h, rest = divmod(ms, 3600000)
    m, rest = divmod(rest, 60000)
    s, ms = divmod(rest, 1000)
    return "%02d:%02d:%02d,%03d" % (h, m, s, ms)


def emit(event):
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


VOICES_LOG = "FAKE_ENGINE_VOICES_LOG"  # env: a file each `voices save` call appends its argv to (JSON lines)
VOICES_FAIL = "FAKE_ENGINE_VOICES_FAIL"  # env: "error" (an error event), "crash" (exit 3) or "hang" (sleep)


def voices_save(argv):
    """``voices save --from OUT --name SPEAKER=NAME ...``: logged to $FAKE_ENGINE_VOICES_LOG, answered like the
    engine (a done event listing what was saved), or failing as $FAKE_ENGINE_VOICES_FAIL says."""
    log = os.environ.get(VOICES_LOG)
    if log:
        with open(log, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(argv) + "\n")
    fail = os.environ.get(VOICES_FAIL)
    if fail == "error":
        emit({"event": "error", "message": "ValueError: voice library is broken"})
        return 1
    if fail == "crash":
        sys.stderr.write("Traceback (most recent call last):\nRuntimeError: boom\n")
        return 3
    if fail == "hang":
        time.sleep(30)
    names = [argv[i + 1].partition("=") for i, arg in enumerate(argv[:-1]) if arg == "--name"]
    emit({"event": "done", "saved": [{"speaker": sid, "name": name} for sid, _sep, name in names], "skipped": []})
    return 0


def write_voices(out, transcript):
    """``--with-voices``: a voices.json with a (fake) centroid for every detected (SPEAKER_NN) speaker."""
    speakers = [s["id"] for s in transcript["speakers"] if s["id"].startswith("SPEAKER_")]
    path = os.path.join(out, "voices.json")
    data = {
        "version": 1,
        "model": "fake-embedding",
        "speakers": {sid: {"source": "mix", "centroid": [1.0, float(i)]} for i, sid in enumerate(speakers)},
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(data, handle)
    return path


def main(argv):
    if argv[:2] == ["voices", "save"]:
        return voices_save(argv)
    if not argv or argv[0] != "run":
        sys.stderr.write("usage: fake_engine.py run AUDIO --out DIR\n")
        return 2
    audio = argv[1] if len(argv) > 1 and not argv[1].startswith("--") else None  # None: --track only
    out = argv[argv.index("--out") + 1]
    slow = "--slow" in argv
    track_specs = [argv[i + 1] for i, arg in enumerate(argv[:-1]) if arg == "--track"]
    if audio is None and not track_specs:
        emit({"event": "error", "message": "no audio and no --track"})
        return 2
    if audio is not None and not os.path.isfile(audio):
        emit({"event": "error", "message": "FileNotFoundError: %s" % audio})
        return 1
    os.makedirs(out, exist_ok=True)
    sys.stderr.write("warning: downloading model (fake)\n")
    sys.stderr.flush()
    print("not json: model loaded")
    sys.stdout.flush()
    for stage, pct in (
        ("transcribe", 10.0),
        ("transcribe", 55.0),
        ("diarize", 85.0),
        ("align", 88.0),
        ("naming", 98.0),
    ):
        emit({"event": "progress", "stage": stage, "pct": pct})
        if slow:
            time.sleep(0.3)
    if "--crash" in argv:
        sys.stderr.write("Traceback (most recent call last):\n  boom\nRuntimeError: engine crashed\n")
        return 3
    if "--fail" in argv:
        emit({"event": "error", "message": "RuntimeError: no speech found"})
        return 1
    transcript = json.loads(json.dumps(TRANSCRIPT))
    fixed = [argv[i + 1] for i, arg in enumerate(argv[:-1]) if arg == "--fixed-track"]
    if fixed:
        transcript = with_fixed_tracks(transcript, fixed)
        if transcript is None:
            return 1
    if track_specs:
        transcript = with_tracks(transcript, track_specs)
        if transcript is None:
            return 1
    if "--with-confidence" in argv:
        transcript = with_confidence(transcript)
    if "--with-words" in argv:
        transcript = with_words(transcript)
    transcript["meta"]["audio"] = audio
    transcript["meta"]["argv"] = argv
    path = os.path.join(out, "transcript.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(transcript, handle, indent=2)
    srt = {}
    by_speaker = {}
    for cue in transcript["cues"]:
        by_speaker.setdefault(cue["speaker"], []).append(cue)
    for speaker_id, cues in by_speaker.items():
        srt_path = os.path.join(out, speaker_id + ".srt")
        blocks = [
            "%d\n%s --> %s\n%s\n" % (i, _ts(c["start"]), _ts(c["end"]), "\n".join(c["lines"]))
            for i, c in enumerate(cues, 1)
        ]
        with open(srt_path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(blocks))
        srt[speaker_id] = srt_path
    voices = write_voices(out, transcript) if "--with-voices" in argv else None
    emit({"event": "progress", "stage": "export", "pct": 100.0})
    emit(
        {
            "event": "done",
            "transcript": path,
            "srt": srt,
            "speakers": [{"id": s["id"], "name": s["name"]} for s in transcript["speakers"]],
            "voices": voices,
        }
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
