"""Progress events while faster-whisper streams segments: monotonic, throttled, inside the transcribe stage."""

import itertools
import sys
import types
from types import SimpleNamespace as NS

import pytest

from better_captions import pipeline
from better_captions.engine import gpu
from better_captions.engine import transcribe as tr

DURATION = 100.0


class Clock:
    """A controllable time.monotonic: every read of ``now`` is the current fake time."""

    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now


def _install(monkeypatch, segment_ends, clock, step_seconds, duration=DURATION):
    """Fake faster-whisper yielding one segment per end time; each segment 'takes' step_seconds of fake time."""

    class Model:
        def __init__(self, *args, **kwargs):
            pass

        def transcribe(self, audio, **kwargs):
            def segments():
                for end in segment_ends:
                    clock.now += step_seconds
                    yield NS(end=end, words=[NS(word=" w", start=max(end - 0.1, 0.0), end=end, probability=0.9)])

            # duration_after_vad is shorter than the file: progress must still use info.duration
            return segments(), NS(language="en", duration=duration, duration_after_vad=duration / 2)

    monkeypatch.setitem(sys.modules, "faster_whisper", types.SimpleNamespace(WhisperModel=Model))
    monkeypatch.setattr(gpu, "setup", lambda: {"dirs": [], "loaded": []})
    monkeypatch.setattr(tr, "_backend_available", lambda name: False)
    monkeypatch.setattr(tr, "_cuda_available", lambda: False)
    monkeypatch.setattr(tr, "time", types.SimpleNamespace(monotonic=clock.monotonic))


def _run():
    seen = []
    tr.transcribe("in.wav", progress=seen.append)
    return seen


def test_events_are_monotonic_and_end_at_one(monkeypatch):
    clock = Clock()
    _install(monkeypatch, [i * 2.0 for i in range(1, 51)], clock, 1.0)  # 50 segments, 1s apart, ends 2..100
    seen = _run()
    assert seen == sorted(seen) and len(set(seen)) == len(seen)
    assert seen[0] == 0.0 and seen[-1] == 1.0
    assert all(0.0 <= f <= 1.0 for f in seen)


def test_fast_segments_are_throttled_by_time(monkeypatch):
    clock = Clock()
    # 100 segments of 1% each, 10ms apart: well under the 0.5s interval, so only first, and the final, get through
    _install(monkeypatch, [float(i) for i in range(1, 101)], clock, 0.01)
    seen = _run()
    assert seen[0] == 0.0 and seen[-1] == 1.0
    assert len(seen) <= 1 + 1 + int(100 * 0.01 / tr.PROGRESS_MIN_INTERVAL) + 1  # 0, ~one per interval, 1.0
    assert len(seen) < 10


def test_tiny_segments_are_throttled_by_step(monkeypatch):
    clock = Clock()
    # 0.1% segments, 1s apart: time never limits, the 1% step does
    _install(monkeypatch, [i * 0.1 for i in range(1, 1001)], clock, 1.0)
    seen = _run()
    assert seen[-1] == 1.0
    gaps = [b - a for a, b in itertools.pairwise(seen[:-1])]
    assert gaps and all(g >= tr.PROGRESS_MIN_STEP - 1e-9 for g in gaps)
    assert len(seen) <= 1 / tr.PROGRESS_MIN_STEP + 3


def test_uses_file_duration_not_vad_duration(monkeypatch):
    clock = Clock()
    _install(monkeypatch, [10.0, 25.0, 100.0], clock, 1.0)  # info.duration_after_vad is 50: must be ignored
    # the first segment is already decoded when the 0.0 start event goes out (same instant), so 0.1 waits
    assert _run() == [0.0, 0.25, 1.0]


def test_zero_duration_reports_nothing(monkeypatch):
    clock = Clock()
    _install(monkeypatch, [1.0, 2.0], clock, 1.0, duration=0.0)
    assert _run() == []


def test_pipeline_maps_events_into_the_transcribe_stage_share(monkeypatch):
    clock = Clock()
    _install(monkeypatch, [i * 2.0 for i in range(1, 51)], clock, 1.0)
    events = []
    tr.transcribe("in.wav", progress=pipeline._stage_progress(events.append, "transcribe"))
    share = 100 * dict(pipeline.STAGES)["transcribe"]  # transcribe is the first stage: offset 0
    pcts = [e["pct"] for e in events]
    assert {e["stage"] for e in events} == {"transcribe"}
    assert pcts == sorted(pcts) and pcts[0] == 0.0
    assert pcts[-1] == pytest.approx(share)
    assert all(0.0 <= p <= share for p in pcts)


def test_throttled_progress_unit(monkeypatch):
    clock = Clock()
    seen = []
    report = tr.ThrottledProgress(seen.append, min_interval=1.0, min_step=0.1, clock=clock.monotonic)
    report(0.0)
    report(0.5)  # same instant: held back by the interval
    clock.now = 2.0
    report(0.55)  # interval and step both satisfied
    clock.now = 4.0
    report(0.6)  # interval ok, step (0.05) too small
    report(0.4)  # going backwards is ignored
    report(1.0)
    report(1.0)  # no repeat
    assert seen == [0.0, 0.55, 1.0]
    tr.ThrottledProgress(None)(0.5)  # no sink: no error
