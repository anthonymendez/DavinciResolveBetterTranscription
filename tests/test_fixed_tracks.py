"""Speakers pinned to their own audio track (--fixed-track NAME=PATH): engine.fixed_tracks and its wiring."""

import json

import numpy as np
import pytest

from better_captions import cli
from better_captions.engine import diarize, fixed_tracks, transcribe
from better_captions.engine.fixed_tracks import FixedTrack
from better_captions.schema import SpeakerTurn, Transcript, Word

SR = fixed_tracks.SAMPLE_RATE
DURATION = 8.0

ANTHONY = [(0.5, 1.5), (4.0, 5.0)]  # loud speech on the microphone track
OTHER = [(2.0, 3.0), (6.0, 7.0)]  # someone else, on another track
WHISPER = (7.3, 7.8)  # Anthony, too quiet for voice activity


def _tone(spans, amplitude, freq):
    t = np.arange(int(DURATION * SR)) / SR
    x = np.zeros_like(t)
    for start, end in spans:
        on = (t >= start) & (t < end)
        x[on] = amplitude * np.sin(2 * np.pi * freq * t[on])
    return x.astype(np.float32)


def _signals(bleed=0.03):
    """(mix, Anthony's mic): the mic also picks up the other speaker faintly."""
    rng = np.random.default_rng(0)
    anthony = _tone(ANTHONY, 0.3, 220) + _tone([WHISPER], 0.002, 240)
    other = _tone(OTHER, 0.3, 150)
    noise = (rng.standard_normal(len(anthony)) * 1e-4).astype(np.float32)
    mic = anthony + bleed * other + noise
    mix = anthony + other + noise
    return mix, mic


def _analysis(**kw):
    mix, mic = _signals(**kw)
    audio = {"mix.wav": mix, "mic.wav": mic}
    return fixed_tracks.analyze("mix.wav", [FixedTrack("Anthony", "mic.wav")], load=audio.__getitem__)


WORDS = [
    Word("Hi", 0.6, 0.9),
    Word("there.", 1.0, 1.4),
    Word("Yo,", 2.1, 2.5),
    Word("what's", 2.5, 2.9),
    Word("up?", 4.2, 4.6),
    Word("Okay.", 6.2, 6.6),
    Word("psst", 7.35, 7.75),
]


# --- parsing


def test_parse_fixed_track_splits_at_the_first_equals_sign():
    assert fixed_tracks.parse_fixed_track("Anthony=C:/a=b/mic.wav") == FixedTrack("Anthony", "C:/a=b/mic.wav")
    assert fixed_tracks.parse_fixed_track(" Mary Jo = /x.wav ") == FixedTrack("Mary Jo", "/x.wav")
    for bad in ("Anthony", "=x.wav", "Anthony=", ""):
        with pytest.raises(ValueError, match="NAME=PATH"):
            fixed_tracks.parse_fixed_track(bad)


def test_cli_parses_repeated_fixed_tracks():
    args = cli.build_parser().parse_args(
        ["run", "mix.wav", "--out", "o", "--fixed-track", "Anthony=mic.wav", "--fixed-track", "Sam=sam.wav"]
    )
    assert args.fixed_track == [FixedTrack("Anthony", "mic.wav"), FixedTrack("Sam", "sam.wav")]
    assert cli.build_parser().parse_args(["run", "mix.wav", "--out", "o"]).fixed_track == []


def test_cli_rejects_a_fixed_track_without_a_name(capsys):
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["run", "mix.wav", "--out", "o", "--fixed-track", "mic.wav"])
    assert "NAME=PATH" in capsys.readouterr().err


def test_tracks_with_the_same_name_share_one_speaker():
    tracks = [FixedTrack("Anthony", "a.wav"), FixedTrack("Sam", "s.wav"), FixedTrack("Anthony", "b.wav")]
    assert fixed_tracks.speaker_ids(tracks) == {"Anthony": "PINNED_00", "Sam": "PINNED_01"}
    speakers = fixed_tracks.fixed_speakers(tracks)
    assert [(s.id, s.name, s.name_source) for s in speakers.values()] == [
        ("PINNED_00", "Anthony", "user"),
        ("PINNED_01", "Sam", "user"),
    ]


# --- analysis


def test_voice_activity_bridges_pauses_and_drops_clicks():
    frame = fixed_tracks.FRAME_SECONDS
    energy = np.full(200, 1e-10)
    energy[20:60] = 1e-2  # 0.8 s of speech
    energy[65:100] = 1e-2  # after a 0.1 s pause
    energy[150:152] = 1e-2  # a 40 ms click
    active = fixed_tracks.voice_activity(energy, frame)
    assert active[20:100].all()
    assert not active[150:152].any()
    assert not active[110:140].any()


def test_words_during_a_pinned_stems_speech_get_the_fixed_speaker():
    labelled = _analysis().label_words(WORDS)
    assert [w.speaker for w in labelled] == ["PINNED_00", "PINNED_00", None, None, "PINNED_00", None, None]


def test_bleed_on_the_microphone_does_not_make_others_the_pinned_speaker():
    analysis = _analysis(bleed=0.2)  # loud enough to pass the microphone's voice activity
    assert analysis.owner(2.1, 2.9) is None
    assert analysis.owner(0.6, 0.9) == "PINNED_00"


def test_the_residual_silences_only_the_pinned_speech(tmp_path):
    analysis = _analysis()
    residual = analysis.residual()

    def rms(start, end):
        return float(np.sqrt(np.mean(residual[int(start * SR) : int(end * SR)] ** 2)))

    assert rms(0.6, 1.4) == 0.0 and rms(4.1, 4.9) == 0.0
    assert rms(2.1, 2.9) > 0.1 and rms(6.1, 6.9) > 0.1
    path = tmp_path / "residual.wav"
    assert analysis.write_residual(str(path))
    reread = diarize.load_audio(str(path))
    assert len(reread) == len(residual) and abs(float(np.abs(reread).max()) - float(np.abs(residual).max())) < 1e-3


def test_nothing_is_left_to_diarize_when_the_pinned_speaker_says_everything(tmp_path):
    anthony = _tone(ANTHONY, 0.3, 220)
    audio = {"mix.wav": anthony, "mic.wav": anthony}
    analysis = fixed_tracks.analyze("mix.wav", [FixedTrack("Anthony", "mic.wav")], load=audio.__getitem__)
    assert analysis.write_residual(str(tmp_path / "r.wav")) is False
    assert not (tmp_path / "r.wav").exists()


def test_a_cluster_that_is_really_the_pinned_speaker_is_merged_into_it():
    turns = [SpeakerTurn("SPEAKER_00", 2.0, 3.0), SpeakerTurn("SPEAKER_01", *WHISPER), SpeakerTurn("SPEAKER_00", 6, 7)]
    merged = _analysis().merge_turns(turns)
    assert [t.speaker for t in merged] == ["SPEAKER_00", "PINNED_00", "SPEAKER_00"]


def test_other_words_are_diarized_from_the_residual_turns():
    from better_captions.engine import align

    turns = [SpeakerTurn("SPEAKER_00", 2.0, 3.0), SpeakerTurn("SPEAKER_00", 6, 7), SpeakerTurn("SPEAKER_01", *WHISPER)]
    words = fixed_tracks.assign_speakers(_analysis(), WORDS, turns, align.assign_speakers)
    assert [w.speaker for w in words] == [
        "PINNED_00",
        "PINNED_00",
        "SPEAKER_00",
        "SPEAKER_00",
        "PINNED_00",
        "SPEAKER_00",
        "PINNED_00",  # the whisper: its cluster was merged into Anthony
    ]


def test_stems_a_little_shorter_or_longer_than_the_mix_are_fitted():
    mix, mic = _signals()
    audio = {"mix.wav": mix, "short.wav": mic[:-500], "long.wav": np.concatenate([mic, np.zeros(900)])}
    for stem in ("short.wav", "long.wav"):
        analysis = fixed_tracks.analyze("mix.wav", [FixedTrack("Anthony", stem)], load=audio.__getitem__)
        assert analysis.owner(0.6, 0.9) == "PINNED_00"


# --- pipeline


def _write(path, samples):
    fixed_tracks.write_wav(str(path), samples)
    return str(path)


def test_cli_run_with_a_fixed_track(tmp_path, monkeypatch, capsys):
    mix, mic = _signals()
    mix_path = _write(tmp_path / "mix.wav", mix)
    mic_path = _write(tmp_path / "mic.wav", mic)
    seen = {}

    def fake_transcribe(audio_path, **kw):
        seen["transcribed"] = audio_path
        return "en", DURATION, list(WORDS)

    def fake_diarize(audio_path, **kw):
        seen["diarized"] = audio_path
        seen["kw"] = kw
        residual = diarize.load_audio(audio_path)
        seen["pinned_rms"] = float(np.sqrt(np.mean(residual[int(0.6 * SR) : int(1.4 * SR)] ** 2)))
        seen["other_rms"] = float(np.sqrt(np.mean(residual[int(2.1 * SR) : int(2.9 * SR)] ** 2)))
        return diarize.DiarizeResult([SpeakerTurn("SPEAKER_00", 2.0, 3.0), SpeakerTurn("SPEAKER_00", 6.0, 7.0)])

    monkeypatch.setattr(transcribe, "transcribe", fake_transcribe)
    monkeypatch.setattr(diarize, "diarize_detailed", fake_diarize)
    out = tmp_path / "out"
    rc = cli.main(
        [
            "run",
            mix_path,
            "--out",
            str(out),
            "--naming",
            "heuristic",
            "--no-cache",
            "--num-speakers",
            "2",
            "--fixed-track",
            "Anthony=" + mic_path,
        ]
    )
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert rc == 0, events
    assert seen["transcribed"] == mix_path  # the mix is transcribed once, as always
    assert seen["diarized"] != mix_path  # ...but only the residual is diarized
    assert seen["pinned_rms"] == 0.0 and seen["other_rms"] > 0.1
    assert seen["kw"]["num_speakers"] == 1  # the pinned speaker counts towards --num-speakers

    t = Transcript.load(out / "transcript.json")
    anthony = t.speaker("PINNED_00")
    assert (anthony.name, anthony.name_source, anthony.name_confidence) == ("Anthony", "user", 1.0)
    by_word = {w.text: w.speaker for s in t.segments for w in s.words}
    assert by_word == {
        "Hi": "PINNED_00",
        "there.": "PINNED_00",
        "Yo,": "SPEAKER_00",
        "what's": "SPEAKER_00",
        "up?": "PINNED_00",
        "Okay.": "SPEAKER_00",
        "psst": "SPEAKER_00",  # nearest residual turn: no cluster here to merge
    }
    assert t.meta["fixed_tracks"] == [{"id": "PINNED_00", "name": "Anthony", "audio": mic_path}]
    assert {"id": "PINNED_00", "name": "Anthony"} in events[-1]["speakers"]
    assert (out / "PINNED_00.srt").is_file()


def test_a_missing_fixed_track_is_reported_as_an_error(tmp_path, monkeypatch, capsys):
    mix_path = _write(tmp_path / "mix.wav", _signals()[0])
    monkeypatch.setattr(transcribe, "transcribe", lambda audio_path, **kw: ("en", DURATION, list(WORDS)))
    monkeypatch.setattr(diarize, "diarize_detailed", lambda audio_path, **kw: pytest.fail("must not diarize"))
    missing = str(tmp_path / "nope.wav")
    rc = cli.main(["run", mix_path, "--out", str(tmp_path / "o"), "--no-cache", "--fixed-track", "A=" + missing])
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert rc == 1 and events[-1]["event"] == "error"
