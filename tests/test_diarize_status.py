"""Speaker detection reports its status (started / models / progress / speakers found / finished) where the
plugin's log window shows engine output: the engine's stderr, which engine_runner turns into `log` events."""

import sys
import textwrap
import types
from types import SimpleNamespace as NS

import numpy as np
import pytest

from better_captions.engine import diarize as dz
from better_captions_plugin.engine_runner import run_engine

AUDIO_SECONDS = 30 * 60
THREE = [(0.0, 2.0, 0), (2.0, 4.0, 1), (4.0, 6.0, 2), (6.0, 7.0, 0)]


class _Config:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

    def validate(self):
        return True


def make_fake_sherpa(results, callbacks=10):
    """results: num_clusters -> [(start, end, speaker)]; process() reports `callbacks` chunks."""
    runs = []

    class OfflineSpeakerDiarization:
        sample_rate = 16000

        def __init__(self, config):
            self.config = config

        def process(self, samples, callback=None):
            num_clusters = self.config.clustering.num_clusters
            runs.append(num_clusters)
            for i in range(1, callbacks + 1):
                callback(i, callbacks)
            segments = [NS(start=s, end=e, speaker=k) for s, e, k in results[num_clusters]]
            return NS(sort_by_start_time=lambda: sorted(segments, key=lambda s: s.start))

    mod = types.SimpleNamespace(
        OfflineSpeakerDiarizationConfig=_Config,
        OfflineSpeakerSegmentationModelConfig=_Config,
        OfflineSpeakerSegmentationPyannoteModelConfig=_Config,
        SpeakerEmbeddingExtractorConfig=_Config,
        FastClusteringConfig=_Config,
        OfflineSpeakerDiarization=OfflineSpeakerDiarization,
    )
    return mod, runs


@pytest.fixture
def sherpa(monkeypatch, tmp_path):
    def install(results=None, callbacks=10):
        mod, runs = make_fake_sherpa(results or {-1: THREE}, callbacks)
        monkeypatch.setitem(sys.modules, "sherpa_onnx", mod)
        return runs

    monkeypatch.setattr(dz, "sherpa_models", lambda: (tmp_path / "seg.onnx", tmp_path / "emb.onnx"))
    # A 30 minute recording, without allocating it.
    monkeypatch.setattr(dz, "load_audio", lambda path, sr=dz.SAMPLE_RATE: np.zeros(AUDIO_SECONDS * sr, np.float32))
    return install


def test_status_lines_go_to_stderr_by_default(sherpa, capsys):
    sherpa()
    dz.diarize("in.wav")
    captured = capsys.readouterr()
    assert captured.out == ""  # stdout carries the JSON events only
    lines = captured.err.splitlines()
    assert lines[0] == "Detecting speakers: started (sherpa backend)"
    assert lines[1] == "Detecting speakers: loading models"
    assert lines[2] == "Detecting speakers: analysing 30:00 of audio"
    assert lines[-2] == "Detecting speakers: found 3 speakers"
    assert lines[-1].startswith("Detecting speakers: finished in ")


def test_progress_is_reported_with_position_in_the_audio(sherpa):
    sherpa()
    lines = []
    dz.diarize("in.wav", log=lines.append)
    assert "Detecting speakers: 40% (12:00 of 30:00)" in lines
    assert lines[-3] == "Detecting speakers: 100% (30:00 of 30:00)"


def test_progress_is_throttled(sherpa):
    sherpa(callbacks=600)  # many tiny chunks must not become 600 lines
    lines = []
    dz.diarize("in.wav", log=lines.append)
    percents = [line for line in lines if "%" in line]
    assert [line.split()[2] for line in percents] == [f"{p}%" for p in range(10, 101, 10)]


def test_messages_in_order_and_speakers_and_elapsed(sherpa, monkeypatch):
    sherpa()
    ticks = iter([100.0, 172.0])  # start, finish
    monkeypatch.setattr(dz.time, "monotonic", lambda: next(ticks))
    lines = []
    dz.diarize("in.wav", log=lines.append)
    kinds = [
        next(k for k in ("started", "loading", "analysing", "%", "found", "finished") if k in line) for line in lines
    ]
    assert kinds == ["started", "loading", "analysing", *["%"] * 10, "found", "finished"]
    assert lines[-2] == "Detecting speakers: found 3 speakers"
    assert lines[-1] == "Detecting speakers: finished in 1m 12s"


def test_single_speaker_is_singular(sherpa):
    sherpa({-1: [(0.0, 5.0, 0)]})
    lines = []
    dz.diarize("in.wav", log=lines.append)
    assert "Detecting speakers: found 1 speaker" in lines


def test_reclustering_is_announced_and_progress_stays_monotonic(sherpa):
    runs = sherpa({-1: THREE, 2: [(0.0, 4.0, 0), (4.0, 7.0, 1)]})
    lines = []
    turns = dz.diarize("in.wav", max_speakers=2, log=lines.append)
    assert runs == [-1, 2]
    assert len({t.speaker for t in turns}) == 2
    assert "Detecting speakers: re-clustering for 2 speakers" in lines
    percents = [int(line.split()[2][:-1]) for line in lines if "%" in line]
    assert percents == sorted(set(percents))
    assert lines[-2] == "Detecting speakers: found 2 speakers"


def test_logging_failure_does_not_fail_the_run(sherpa, monkeypatch):
    sherpa()

    def broken(*args, **kwargs):
        raise OSError("stderr gone")

    monkeypatch.setattr("builtins.print", broken)
    assert len(dz.diarize("in.wav")) == 4


def test_clock_formatting():
    assert dz.format_clock(0) == "0:00"
    assert dz.format_clock(719.6) == "12:00"
    assert dz.format_clock(3723) == "1:02:03"
    assert dz.format_elapsed(4.26) == "4.3s"
    assert dz.format_elapsed(125) == "2m 05s"


def test_status_reaches_the_plugin_log_in_a_normal_run(tmp_path):
    """Run diarize() in a real engine process and read it back through the plugin's own runner: the lines
    arrive as `log` events without --verbose or any logging setup."""
    script = textwrap.dedent(
        f"""
        import sys, types
        import numpy as np
        from better_captions.engine import diarize as dz
        from types import SimpleNamespace as NS

        class C:
            def __init__(self, **kw): self.__dict__.update(kw)
            def validate(self): return True

        class SD:
            sample_rate = 16000
            def __init__(self, config): self.config = config
            def process(self, samples, callback=None):
                for i in range(1, 5):
                    callback(i, 4)
                seg = [NS(start=0.0, end=2.0, speaker=0), NS(start=2.0, end=4.0, speaker=1)]
                return NS(sort_by_start_time=lambda: seg)

        sys.modules["sherpa_onnx"] = types.SimpleNamespace(
            OfflineSpeakerDiarizationConfig=C, OfflineSpeakerSegmentationModelConfig=C,
            OfflineSpeakerSegmentationPyannoteModelConfig=C, SpeakerEmbeddingExtractorConfig=C,
            FastClusteringConfig=C, OfflineSpeakerDiarization=SD)
        dz.sherpa_models = lambda: ("seg.onnx", "emb.onnx")
        dz.load_audio = lambda path, sr=16000: np.zeros({AUDIO_SECONDS} * sr, np.float32)
        dz.diarize("in.wav")
        print('{{"event": "done"}}')
        """
    )
    events = []
    run_engine([sys.executable, "-c", script], on_event=events.append)
    logs = [e["line"] for e in events if e["event"] == "log"]
    assert logs[0] == "Detecting speakers: started (sherpa backend)"
    assert "Detecting speakers: loading models" in logs
    assert "Detecting speakers: 50% (15:00 of 30:00)" in logs
    assert "Detecting speakers: found 2 speakers" in logs
    assert logs[-1].startswith("Detecting speakers: finished in ")
