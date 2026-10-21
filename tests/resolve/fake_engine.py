"""A stand-in for `better-captions run` used as a real subprocess by the plugin tests.

Usage: python fake_engine.py run AUDIO --out DIR [--fail] [--crash] [--slow] [...ignored flags]

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


def _ts(seconds):
    ms = int(round(seconds * 1000))
    h, rest = divmod(ms, 3600000)
    m, rest = divmod(rest, 60000)
    s, ms = divmod(rest, 1000)
    return "%02d:%02d:%02d,%03d" % (h, m, s, ms)


def emit(event):
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


def main(argv):
    if not argv or argv[0] != "run":
        sys.stderr.write("usage: fake_engine.py run AUDIO --out DIR\n")
        return 2
    audio = argv[1]
    out = argv[argv.index("--out") + 1]
    slow = "--slow" in argv
    if not os.path.isfile(audio):
        emit({"event": "error", "message": "FileNotFoundError: %s" % audio})
        return 1
    os.makedirs(out, exist_ok=True)
    sys.stderr.write("warning: downloading model (fake)\n")
    sys.stderr.flush()
    print("not json: model loaded")
    sys.stdout.flush()
    for stage, pct in (("transcribe", 10.0), ("transcribe", 55.0), ("diarize", 85.0), ("align", 88.0), ("naming", 98.0)):
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
        blocks = ["%d\n%s --> %s\n%s\n" % (i, _ts(c["start"]), _ts(c["end"]), "\n".join(c["lines"])) for i, c in enumerate(cues, 1)]
        with open(srt_path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(blocks))
        srt[speaker_id] = srt_path
    emit({"event": "progress", "stage": "export", "pct": 100.0})
    emit({"event": "done", "transcript": path, "srt": srt, "speakers": [{"id": s["id"], "name": s["name"]} for s in transcript["speakers"]]})
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
