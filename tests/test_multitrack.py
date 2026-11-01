"""Each audio track transcribed on its own (--track ROLE:NAME=PATH): engine.multitrack and pipeline wiring."""

import json

import numpy as np
import pytest

from better_captions import cli
from better_captions.engine import diarize, fixed_tracks, multitrack, transcribe
from better_captions.engine.multitrack import GROUP, NONE, PERSON, Track
from better_captions.schema import SpeakerTurn, Transcript, Word

SR = 16000
DURATION = 6.0


def _tone(spans, amplitude, freq):
    t = np.arange(int(DURATION * SR)) / SR
    x = np.zeros_like(t)
    for start, end in spans:
        on = (t >= start) & (t < end)
        x[on] = amplitude * np.sin(2 * np.pi * freq * t[on])
    return x.astype(np.float32)


def _write(path, samples):
    fixed_tracks.write_wav(str(path), samples, SR)
    return str(path)


# --- parsing


def test_parse_track_roles_names_and_paths():
    assert multitrack.parse_track("person:Anthony=C:/a=b/mic.wav") == Track(PERSON, "Anthony", "C:/a=b/mic.wav")
    assert multitrack.parse_track("group:Discord=d.wav") == Track(GROUP, "Discord", "d.wav")
    assert multitrack.parse_track(" GROUP = d.wav ") == Track(GROUP, "", "d.wav")
    assert multitrack.parse_track("none:Game audio=g.wav") == Track(NONE, "Game audio", "g.wav")
    assert multitrack.parse_track("Mary Jo=/x.wav") == Track(PERSON, "Mary Jo", "/x.wav")  # as --fixed-track
    assert multitrack.parse_track("person:Dr: Who=w.wav") == Track(PERSON, "Dr: Who", "w.wav")
    for bad in ("person:=mic.wav", "group:Discord", "group:Discord=", "robot:R2=r.wav", "=x.wav"):
        with pytest.raises(ValueError):
            multitrack.parse_track(bad)


def test_person_tracks_share_pinned_ids_by_name_and_are_named_by_the_user():
    tracks = [Track(GROUP, "Discord", "d"), Track(PERSON, "Anthony", "a"), Track(PERSON, "Sam", "s")]
    assert multitrack.person_ids(tracks) == {"Anthony": "PINNED_00", "Sam": "PINNED_01"}
    anthony = multitrack.person_speakers(tracks)["PINNED_00"]
    assert (anthony.name, anthony.name_source, anthony.name_confidence) == ("Anthony", "user", 1.0)
    assert multitrack.describe(tracks)[1] == {"role": PERSON, "name": "Anthony", "audio": "a", "id": "PINNED_00"}


# --- speakers of group tracks


def test_group_speakers_are_numbered_across_group_tracks():
    numbering = multitrack.GroupNumbering()
    first = numbering.assign(
        [Word("a", 0.0, 0.5), Word("b", 2.0, 2.5)],
        [SpeakerTurn("SPEAKER_01", 0.0, 1.0), SpeakerTurn("SPEAKER_00", 2.0, 3.0), SpeakerTurn("SPEAKER_02", 5, 6)],
    )
    second = numbering.assign([Word("c", 0.0, 0.5)], [SpeakerTurn("SPEAKER_00", 0.0, 1.0)])
    silent = numbering.assign([Word("d", 1.0, 1.5)], [])
    assert [w.speaker for w in first] == ["SPEAKER_00", "SPEAKER_01"]
    assert [w.speaker for w in second] == ["SPEAKER_02"]
    assert [w.speaker for w in silent] == ["SPEAKER_03"]


# --- bleed


def _tagged(*items):
    return [(track, Word(text, start, start + 0.3, probability=p)) for track, text, start, p in items]


def test_the_same_word_on_two_tracks_is_kept_where_it_is_louder():
    words = _tagged((0, "Okay.", 1.0, 0.9), (1, "okay", 1.1, 0.9), (0, "hi", 3.0, 0.9))
    levels = {0: -45.0, 1: -12.0}
    kept, dropped = multitrack.drop_bleed(words, [PERSON, GROUP], lambda track, s, e: levels[track])
    assert dropped == 1
    assert [(t, w.text) for t, w in kept] == [(1, "okay"), (0, "hi")]


def test_bleed_too_close_to_call_goes_to_the_person_track():
    words = _tagged((0, "hey", 1.0, 0.5), (1, "hey", 1.05, 0.99))
    kept, dropped = multitrack.drop_bleed(words, [GROUP, PERSON], lambda track, s, e: -20.0 + track)
    assert dropped == 1 and [t for t, _w in kept] == [1]
    kept, _ = multitrack.drop_bleed(words, [GROUP, PERSON], None)  # levels unknown
    assert [t for t, _w in kept] == [1]


def test_different_words_or_times_are_not_bleed():
    words = _tagged((0, "yes", 1.0, 0.9), (1, "no", 1.0, 0.9), (0, "go", 3.0, 0.9), (1, "go", 4.0, 0.9))
    kept, dropped = multitrack.drop_bleed(words, [PERSON, GROUP], lambda t, s, e: 0.0)
    assert dropped == 0 and len(kept) == 4


def test_a_phrase_of_bleed_is_decided_as_a_whole():
    # one word measures louder on the mic, but the phrase as a whole is clearly Discord's
    words = _tagged(
        (0, "we", 1.0, 0.9), (1, "we", 1.0, 0.9),
        (0, "should", 1.3, 0.9), (1, "should", 1.3, 0.9),
        (0, "go", 1.6, 0.9), (1, "go", 1.6, 0.9),
    )  # fmt: skip
    measured = {(0, 1.0): -10.0, (1, 1.0): -14.0, (0, 1.3): -40.0, (1, 1.3): -12.0, (0, 1.6): -40.0, (1, 1.6): -12.0}
    kept, dropped = multitrack.drop_bleed(words, [PERSON, GROUP], lambda t, s, e: measured[(t, round(s, 1))])
    assert dropped == 3 and {t for t, _w in kept} == {1}


def test_track_levels_reads_spans_of_wav_files(tmp_path):
    loud = _write(tmp_path / "loud.wav", _tone([(1.0, 2.0)], 0.5, 220))
    levels = multitrack.TrackLevels([loud, str(tmp_path / "missing.wav")])
    try:
        assert levels(0, 1.2, 1.8) == pytest.approx(10 * np.log10(0.125), abs=0.5)
        assert levels(0, 3.0, 3.5) == multitrack.SILENCE_DB
        assert levels(0, 100.0, 101.0) == multitrack.SILENCE_DB
        assert levels(1, 0.0, 1.0) is None
    finally:
        levels.close()


def test_overlapping_speakers_keep_their_sentences_whole():
    words = [
        Word("I", 0.0, 0.2, speaker="A"),
        Word("think", 0.3, 0.5, speaker="A"),
        Word("wait", 0.4, 0.6, speaker="B"),
        Word("so.", 0.7, 0.9, speaker="A"),
    ]
    segments = multitrack.group_segments(words)
    assert [(s.speaker, s.text) for s in segments] == [("A", "I think so."), ("B", "wait")]


# --- end to end: a microphone (one person) and a Discord call (two people)

MIC_WORDS = [
    Word("Hi", 0.5, 0.8),
    Word("there.", 0.9, 1.3),
    Word("Wait,", 2.1, 2.5),
    Word("what?", 2.6, 2.9),
    Word("Okay.", 4.25, 4.55),  # the Discord friend, heard faintly on the microphone
]
DISCORD_WORDS = [
    Word("hi", 0.55, 0.8),  # Anthony, heard faintly through Discord
    Word("Look", 2.2, 2.5),
    Word("out!", 2.6, 3.0),
    Word("Okay.", 4.2, 4.5),
    Word("Sure.", 4.7, 4.95),
]


@pytest.fixture
def two_tracks(tmp_path, monkeypatch):
    anthony = [(0.5, 1.3), (2.1, 2.9)]
    friends = [(2.2, 3.0), (4.2, 5.0)]
    mic = _tone(anthony, 0.3, 220) + _tone(friends, 0.01, 150)
    discord = _tone(friends, 0.3, 150) + _tone(anthony, 0.01, 220)
    paths = {
        "mic": _write(tmp_path / "mic.wav", mic),
        "discord": _write(tmp_path / "discord.wav", discord),
        "game": _write(tmp_path / "game.wav", _tone([(0.0, 6.0)], 0.3, 440)),
    }
    seen = {"transcribed": [], "diarized": []}

    def fake_transcribe(audio_path, progress=None, **kw):
        seen["transcribed"].append(audio_path)
        if progress:
            progress(0.5)
        words = MIC_WORDS if audio_path == paths["mic"] else DISCORD_WORDS
        return "en", DURATION, [Word(w.text, w.start, w.end) for w in words]

    def fake_diarize(audio_path, **kw):
        seen["diarized"].append((audio_path, kw))
        return diarize.DiarizeResult([SpeakerTurn("SPEAKER_00", 2.0, 3.1), SpeakerTurn("SPEAKER_01", 4.1, 5.0)])

    monkeypatch.setattr(transcribe, "transcribe", fake_transcribe)
    monkeypatch.setattr(diarize, "diarize_detailed", fake_diarize)
    return paths, seen


def _run(tmp_path, capsys, *args):
    out = tmp_path / "out"
    rc = cli.main(["run", "--out", str(out), "--naming", "heuristic", "--no-cache", *args])
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert rc == 0, events
    return Transcript.load(out / "transcript.json"), events


def test_person_and_group_tracks_overlap_drop_bleed_and_skip_non_speech(tmp_path, capsys, two_tracks):
    paths, seen = two_tracks
    t, events = _run(
        tmp_path,
        capsys,
        "--num-speakers",
        "3",
        "--track",
        "person:Anthony=" + paths["mic"],
        "--track",
        "group:Discord=" + paths["discord"],
        "--track",
        "none:Game=" + paths["game"],
    )
    assert seen["transcribed"] == [paths["mic"], paths["discord"]]  # the game track is never transcribed
    ((diarized, kw),) = seen["diarized"]  # only the group track goes through speaker detection
    assert diarized == paths["discord"]
    assert kw["num_speakers"] == 2  # Anthony counts towards --num-speakers

    words = [(w.text, w.speaker) for s in t.segments for w in s.words]
    assert sorted(words, key=lambda tw: tw[0]) == sorted(
        [
            ("Hi", "PINNED_00"),
            ("there.", "PINNED_00"),
            ("Wait,", "PINNED_00"),  # Anthony and the friend talk at once: both are captioned
            ("what?", "PINNED_00"),
            ("Look", "SPEAKER_00"),
            ("out!", "SPEAKER_00"),
            ("Okay.", "SPEAKER_01"),  # from Discord, where it is loud; the microphone's copy is bleed
            ("Sure.", "SPEAKER_01"),
        ],
        key=lambda tw: tw[0],
    )
    assert t.meta["bleed_dropped"] == 2
    assert t.meta["audio"] is None
    assert [(m["role"], m["name"]) for m in t.meta["tracks"]] == [
        (PERSON, "Anthony"),
        (GROUP, "Discord"),
        (NONE, "Game"),
    ]
    anthony = t.speaker("PINNED_00")
    assert (anthony.name, anthony.name_source) == ("Anthony", "user")
    cue_speakers = [c.speaker for c in t.cues if 2.0 <= c.start < 3.0]
    assert sorted(cue_speakers) == ["PINNED_00", "SPEAKER_00"]
    assert (tmp_path / "out" / "PINNED_00.srt").is_file()

    progress = [e for e in events if e["event"] == "progress"]
    pcts = [e["pct"] for e in progress]
    assert pcts == sorted(pcts) and pcts[-1] == 100.0
    assert {e.get("track") for e in progress if e["stage"] == "transcribe"} >= {"Anthony", "Discord"}
    assert {e.get("track") for e in progress if e["stage"] == "diarize"} >= {"Discord"}


def test_only_person_tracks_need_no_speaker_detection(tmp_path, capsys, two_tracks, monkeypatch):
    paths, seen = two_tracks
    t, _events = _run(
        tmp_path, capsys, "--track", "person:Anthony=" + paths["mic"], "--fixed-track", "Sam=" + paths["discord"]
    )
    assert seen["diarized"] == []
    speakers = {s.id: s.name for s in t.speakers}
    assert speakers == {"PINNED_00": "Sam", "PINNED_01": "Anthony"}  # --fixed-track first, then --track


def test_two_group_tracks_get_distinct_speakers(tmp_path, capsys, two_tracks):
    paths, seen = two_tracks
    t, _events = _run(
        tmp_path,
        capsys,
        "--num-speakers",
        "4",
        "--track",
        "group=" + paths["mic"],
        "--track",
        "group=" + paths["discord"],
    )
    assert [p for p, _kw in seen["diarized"]] == [paths["mic"], paths["discord"]]
    assert all(kw["num_speakers"] is None and kw["max_speakers"] == 4 for _p, kw in seen["diarized"])
    # mic: SPEAKER_00 and SPEAKER_01 (whose only word, "Okay.", is Discord's bleed and dropped); Discord: 02, 03
    assert {s.id for s in t.speakers} == {"SPEAKER_00", "SPEAKER_02", "SPEAKER_03"}


def test_per_track_results_are_cached_per_track(tmp_path, capsys, two_tracks, monkeypatch):
    paths, seen = two_tracks
    monkeypatch.setattr("better_captions.engine.cache.cache_dir", lambda: tmp_path / "cache")
    args = ["--track", "person:Anthony=" + paths["mic"], "--track", "group:Discord=" + paths["discord"]]
    for _ in range(2):
        rc = cli.main(["run", "--out", str(tmp_path / "out"), "--naming", "none", *args])
        assert rc == 0
    capsys.readouterr()
    assert len(seen["transcribed"]) == 2 and len(seen["diarized"]) == 1  # the second run came from the cache


def test_a_missing_track_is_reported_as_an_error(tmp_path, capsys, two_tracks):
    paths, _seen = two_tracks
    missing = str(tmp_path / "nope.wav")
    rc = cli.main(
        ["run", "--out", str(tmp_path / "o"), "--track", "person:A=" + paths["mic"], "--track", "group=" + missing]
    )
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert rc == 1 and events[-1]["event"] == "error" and "nope.wav" in events[-1]["message"]


def test_no_speech_track_is_an_error(tmp_path, capsys, two_tracks):
    paths, _seen = two_tracks
    rc = cli.main(["run", "--out", str(tmp_path / "o"), "--track", "none:Game=" + paths["game"]])
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert rc == 1 and "no speech track" in events[-1]["message"]
