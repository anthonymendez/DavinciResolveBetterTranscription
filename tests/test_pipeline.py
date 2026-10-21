"""End-to-end pipeline with the heavy model stages faked; naming/cues/srt are real."""

import json

from better_captions import cli
from better_captions.engine import diarize, transcribe
from better_captions.schema import SpeakerTurn, Transcript, Word

WORDS = [
    ("Hey", 0.0),
    ("Jordan,", 0.3),
    ("how", 0.7),
    ("are", 0.9),
    ("you?", 1.1),
    ("Good,", 2.0),
    ("thanks", 2.3),
    ("Maria.", 2.6),
]


def fake_transcribe(audio_path, **kw):
    return "en", 3.5, [Word(t, s, s + 0.25) for t, s in WORDS]


def fake_diarize(audio_path, **kw):
    return [SpeakerTurn("SPEAKER_00", 0.0, 1.5), SpeakerTurn("SPEAKER_01", 1.9, 3.0)]


def test_cli_run_end_to_end(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(transcribe, "transcribe", fake_transcribe)
    monkeypatch.setattr(diarize, "diarize", fake_diarize)
    rc = cli.main(["run", "in.wav", "--out", str(tmp_path), "--naming", "heuristic"])
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert rc == 0, events
    assert events[-1]["event"] == "done"
    pcts = [e["pct"] for e in events if e["event"] == "progress"]
    assert pcts == sorted(pcts) and pcts[-1] == 100.0

    t = Transcript.load(tmp_path / "transcript.json")
    assert [s.id for s in t.speakers] == ["SPEAKER_00", "SPEAKER_01"]
    # Each speaker addresses the other by name.
    assert t.speaker("SPEAKER_00").name == "Maria"
    assert t.speaker("SPEAKER_01").name == "Jordan"
    assert {c.speaker for c in t.cues} == {"SPEAKER_00", "SPEAKER_01"}
    assert "Jordan" in (tmp_path / "SPEAKER_00.srt").read_text()
    assert "Maria" in (tmp_path / "SPEAKER_01.srt").read_text()
