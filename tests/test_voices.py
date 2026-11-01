"""The voice library (engine.voices): saving and matching voices, the CLI, and the pipeline naming
speakers by voice before the transcript namer runs."""

import json

import numpy as np
import pytest

from better_captions import cli, naming, pipeline
from better_captions.engine import cache, diarize, transcribe, voices
from better_captions.pipeline import RunOptions
from better_captions.schema import Segment, Speaker, SpeakerTurn, Transcript, Word

MODEL = diarize.EMBEDDING_NAME
DIM = 64


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("BETTER_CAPTIONS_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


@pytest.fixture
def library(home):
    return voices.VoiceLibrary()


def voice(seed: int) -> np.ndarray:
    """A random unit vector: two different seeds are nearly orthogonal (different people)."""
    v = np.random.default_rng(seed).standard_normal(DIM)
    return v / np.linalg.norm(v)


def heard(base: np.ndarray, noise: float, seed: int = 99) -> np.ndarray:
    """``base`` heard again: the same voice plus some noise (cosine ~ 1 / sqrt(1 + noise^2))."""
    v = base + noise * np.random.default_rng(seed).standard_normal(DIM) / np.sqrt(DIM)
    return v / np.linalg.norm(v)


def between(a: np.ndarray, b: np.ndarray, t: float) -> np.ndarray:
    v = (1 - t) * a + t * b
    return v / np.linalg.norm(v)


ANTHONY, SAM, JORDAN = voice(1), voice(2), voice(3)


# --- the library --------------------------------------------------------------


def test_save_then_match_round_trip(library, home):
    library.save("Anthony", ANTHONY, MODEL, "Microphone")
    library.save("Sam", SAM, MODEL, "Discord")
    assert (home / "voices" / "anthony.json").is_file() and (home / "voices" / "sam.json").is_file()

    people = voices.VoiceLibrary().people()  # read back from disk
    assert [p.name for p in people] == ["Anthony", "Sam"]
    found = voices.match({"SPEAKER_00": heard(SAM, 0.3), "SPEAKER_01": heard(ANTHONY, 0.3, 7)}, MODEL, people)
    assert {sid: m.name for sid, m in found.items()} == {"SPEAKER_00": "Sam", "SPEAKER_01": "Anthony"}
    assert found["SPEAKER_00"].score > 0.9 and found["SPEAKER_00"].second is not None
    assert 0.9 < voices.confidence(found["SPEAKER_00"]) <= 1.0


def test_an_unknown_voice_is_not_matched(library):
    library.save("Anthony", ANTHONY, MODEL)
    assert voices.match({"SPEAKER_00": JORDAN}, MODEL, library.people()) == {}


def test_the_threshold_decides(library):
    library.save("Anthony", ANTHONY, MODEL)
    other = voice(50)
    # find a blend just under and just over the threshold
    near = [between(ANTHONY, other, t) for t in np.linspace(0, 1, 401)]
    sims = [float(v @ ANTHONY) for v in near]
    over = near[max(i for i, s in enumerate(sims) if s >= voices.MATCH_THRESHOLD + 0.01)]
    under = near[min(i for i, s in enumerate(sims) if s < voices.MATCH_THRESHOLD - 0.01)]
    people = library.people()
    assert voices.match({"S": over}, MODEL, people)["S"].name == "Anthony"
    assert voices.match({"S": under}, MODEL, people) == {}
    assert voices.match({"S": under}, MODEL, people, threshold=0.0)["S"].name == "Anthony"


def test_an_ambiguous_match_is_left_for_the_namer(library):
    """Two saved people sound alike: a voice close to both is not given either name."""
    twin = heard(ANTHONY, 0.35, 5)
    library.save("Anthony", ANTHONY, MODEL)
    library.save("Twin", twin, MODEL)
    both = between(ANTHONY, twin, 0.5)
    people = library.people()
    ranked = voices.score_people(both, people, MODEL)
    assert ranked[0][0] >= voices.MATCH_THRESHOLD and ranked[0][0] - ranked[1][0] < voices.MATCH_MARGIN
    assert voices.match({"S": both}, MODEL, people) == {}


def test_voices_of_another_embedding_model_are_ignored(library):
    library.save("Anthony", ANTHONY, "some-other-model.onnx")
    assert voices.match({"S": ANTHONY}, MODEL, library.people()) == {}
    assert voices.match({"S": ANTHONY}, "some-other-model.onnx", library.people())["S"].name == "Anthony"
    # a vector of another size (another model under the same id) is ignored too, not an error
    assert voices.match({"S": np.ones(DIM * 2)}, "some-other-model.onnx", library.people()) == {}


def test_a_person_names_one_speaker_and_never_one_already_named(library):
    library.save("Anthony", ANTHONY, MODEL)
    found = voices.match({"A": heard(ANTHONY, 0.2), "B": heard(ANTHONY, 0.6, 8)}, MODEL, library.people())
    assert list(found) == ["A"]  # the closer one
    taken = voices.match({"A": heard(ANTHONY, 0.2)}, MODEL, library.people(), taken=["anthony"])
    assert taken == {}  # e.g. Anthony's own microphone track is already in the transcript


def test_names_that_differ_only_in_case_are_one_person(library, home):
    library.save("anthony", ANTHONY, MODEL, "Microphone")
    library.save("  Anthony ", heard(ANTHONY, 0.2), MODEL, "Discord")
    (person,) = library.people()
    assert person.name == "Anthony"  # the latest spelling
    assert sorted(c.source for c in person.centroids) == ["Discord", "Microphone"]
    assert [p.name for p in home.joinpath("voices").iterdir()] == ["anthony.json"]


def test_names_with_the_same_file_name_get_their_own_files(library, home):
    library.save("A/B", ANTHONY, MODEL)
    library.save("A_B", SAM, MODEL)
    library.save("A?B", JORDAN, MODEL)
    files = sorted(p.name for p in (home / "voices").iterdir())
    assert files == ["a_b-2.json", "a_b-3.json", "a_b.json"]
    assert [p.name for p in library.people()] == ["A/B", "A?B", "A_B"]
    found = voices.match({"x": SAM, "y": ANTHONY, "z": JORDAN}, MODEL, library.people())
    assert {k: m.name for k, m in found.items()} == {"x": "A_B", "y": "A/B", "z": "A?B"}
    # saving again finds each person's own file
    library.save("A_B", heard(SAM, 0.1), MODEL)
    assert len(list((home / "voices").iterdir())) == 3


def test_file_names_are_safe_on_every_os():
    assert voices.safe_name("Anthony Mendez") == "anthony_mendez"
    assert voices.safe_name("José") == "josé"
    assert voices.safe_name("../../etc") == "etc"
    assert voices.safe_name("CON") == "_con" and voices.safe_name("nul.txt") == "_nul.txt"
    assert voices.safe_name("???") == "voice"
    assert len(voices.safe_name("x" * 300)) == 64


def test_same_source_refines_one_centroid_and_sources_are_capped(library):
    library.save("Sam", SAM, MODEL, "Discord", now=1)
    library.save("Sam", heard(SAM, 0.5), MODEL, "Discord", now=2)
    (centroid,) = library.find("sam").centroids
    assert centroid.count == 2 and centroid.updated == 2
    assert np.isclose(np.linalg.norm(centroid.vector), 1.0) and float(centroid.vector @ SAM) > 0.9

    for i in range(voices.MAX_CENTROIDS + 2):
        library.save("Sam", heard(SAM, 0.3, i), MODEL, f"source {i}", now=10 + i)
    library.save("Sam", SAM, "other-model", "Discord", now=100)  # other models have their own cap
    sam = library.find("Sam")
    kept = [c.source for c in sam.for_model(MODEL)]
    assert len(kept) == voices.MAX_CENTROIDS
    assert "Discord" not in kept and "source 0" not in kept  # the least recently updated went first
    assert f"source {voices.MAX_CENTROIDS + 1}" in kept
    assert [c.source for c in sam.for_model("other-model")] == ["Discord"]


def test_forget_and_unreadable_files(library, home):
    library.save("Sam", SAM, MODEL)
    library.save("Anthony", ANTHONY, MODEL)
    (home / "voices" / "junk.json").write_text("{not json")
    (home / "voices" / "future.json").write_text(json.dumps({"version": 99, "name": "Future", "centroids": []}))
    assert [p.name for p in library.people()] == ["Anthony", "Sam"]
    assert library.forget("SAM") is True
    assert library.forget("Sam") is False
    assert [p.name for p in library.people()] == ["Anthony"]


def test_bad_saves_are_refused(library):
    with pytest.raises(ValueError):
        library.save("  ", SAM, MODEL)
    with pytest.raises(ValueError):
        library.save("Sam", np.zeros(DIM), MODEL)
    assert library.people() == []


# --- a run's voices.json and `voices save` ---------------------------------------


def test_run_voices_round_trip_and_save_confirmed(tmp_path, library):
    out = tmp_path / "out"
    out.mkdir()
    run = {"SPEAKER_00": voices.RunVoice(SAM * 3, "Discord"), "SPEAKER_01": voices.RunVoice(JORDAN, "Discord")}
    path = voices.write_run_voices(out, MODEL, run)
    assert path == out / voices.VOICES_FILE
    model, read = voices.read_run_voices(out)
    assert model == MODEL and set(read) == {"SPEAKER_00", "SPEAKER_01"}
    assert np.allclose(read["SPEAKER_00"].centroid, SAM, atol=1e-5) and read["SPEAKER_00"].source == "Discord"

    saved, skipped = voices.save_confirmed(out, {"SPEAKER_00": "Sam", "PINNED_00": "Anthony"}, library)
    assert saved == [{"speaker": "SPEAKER_00", "name": "Sam", "source": "Discord"}]
    assert skipped == [{"speaker": "PINNED_00", "name": "Anthony", "reason": "no voice for this speaker"}]
    assert [p.name for p in library.people()] == ["Sam"]  # SPEAKER_01 was not confirmed: not saved

    assert voices.write_run_voices(out, MODEL, {}) is None and not path.exists()
    with pytest.raises(ValueError, match="missing"):
        voices.read_run_voices(out)


def test_cli_voices_save_list_forget(tmp_path, capsys, home):
    out = tmp_path / "out"
    out.mkdir()
    voices.write_run_voices(out, MODEL, {"SPEAKER_00": voices.RunVoice(SAM, "Discord")})

    rc = cli.main(["voices", "save", "--from", str(out), "--name", "SPEAKER_00=Sam  Lee", "--name", "SPEAKER_09=X"])
    event = json.loads(capsys.readouterr().out)
    assert rc == 0 and event["event"] == "done"
    assert event["saved"] == [{"speaker": "SPEAKER_00", "name": "Sam Lee", "source": "Discord"}]
    assert [s["speaker"] for s in event["skipped"]] == ["SPEAKER_09"]

    assert cli.main(["voices", "list"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [(p["name"], [v["source"] for v in p["voices"]]) for p in listed["people"]] == [("Sam Lee", ["Discord"])]

    assert cli.main(["voices", "forget", "sam lee"]) == 0
    assert json.loads(capsys.readouterr().out)["removed"] is True
    assert cli.main(["voices", "forget", "sam lee"]) == 1
    capsys.readouterr()
    assert voices.VoiceLibrary().people() == []


def test_cli_voices_save_reports_errors_as_an_event(tmp_path, capsys):
    rc = cli.main(["voices", "save", "--from", str(tmp_path), "--name", "SPEAKER_00=Sam"])
    event = json.loads(capsys.readouterr().out)
    assert rc == 1 and event["event"] == "error" and "missing" in event["message"]
    with pytest.raises(SystemExit):
        cli.main(["voices", "save", "--from", str(tmp_path), "--name", "SPEAKER_00"])


# --- naming keeps voice names ------------------------------------------------------


def test_transcript_namers_keep_voice_named_speakers():
    segments = [
        Segment("SPEAKER_00", 0.0, 1.0, "Hey Jordan, how are you?"),
        Segment("SPEAKER_01", 1.0, 2.0, "Good, thanks Maria."),
    ]
    speakers = [Speaker("SPEAKER_00", "Sam", 0.8, "voice"), Speaker("SPEAKER_01")]
    t = Transcript("en", 2.0, speakers=speakers, segments=segments)
    named = naming.name_speakers(t, provider="heuristic")
    assert (named[0].name, named[0].name_source, named[0].name_confidence) == ("Sam", "voice", 0.8)
    assert named[1].name == "Jordan"  # addressed by SPEAKER_00; "Maria" can't be SPEAKER_00, who is Sam

    from better_captions.naming import llm

    applied = naming._apply_llm(speakers, {"SPEAKER_00": llm.LLMName("Maria", 0.9, ""), "SPEAKER_01": None})
    assert (applied[0].name, applied[0].name_source) == ("Sam", "voice")


# --- the pipeline -------------------------------------------------------------------

WORDS = [
    Word("Hey", 0.0, 0.25),
    Word("Jordan,", 0.3, 0.55),
    Word("Good,", 2.0, 2.25),
    Word("thanks", 2.3, 2.55),
    Word("Maria.", 2.6, 2.85),
    Word("Yo.", 4.0, 4.25),
]
TURNS = [SpeakerTurn("SPEAKER_00", 0.0, 1.5), SpeakerTurn("SPEAKER_01", 1.9, 3.0), SpeakerTurn("SPEAKER_02", 3.9, 4.5)]


@pytest.fixture
def fake_stages(monkeypatch, tmp_path):
    """Fake transcription and a diarization that returns a voice centroid per speaker."""
    calls = {"diarize": 0}
    centroids = {"SPEAKER_00": heard(SAM, 0.2), "SPEAKER_01": voice(40), "SPEAKER_02": heard(JORDAN, 0.2, 4)}

    def fake_diarize(audio_path, **kw):
        calls["diarize"] += 1
        return diarize.DiarizeResult(list(TURNS), dict(centroids), [None] * len(TURNS), MODEL)

    monkeypatch.setattr(
        transcribe, "transcribe", lambda audio_path, **kw: ("en", 5.0, [Word(**vars(w)) for w in WORDS])
    )
    monkeypatch.setattr(diarize, "diarize_detailed", fake_diarize)
    audio = tmp_path / "in.wav"
    audio.write_bytes(b"RIFF fake audio " * 100)
    return audio, calls, centroids


def _run(audio, out, **opts):
    events = []
    transcript = pipeline.run(str(audio), out, RunOptions(naming_provider="heuristic", **opts), emit=events.append)
    return transcript, events


def test_pipeline_names_known_voices_before_the_namer_and_writes_every_voice(tmp_path, fake_stages, library):
    audio, _calls, centroids = fake_stages
    library.save("Sam", SAM, MODEL, "Discord")
    library.save("Jordan", JORDAN, "another-model")  # not comparable: SPEAKER_02 stays unknown

    t, events = _run(audio, tmp_path / "out")
    sam = t.speaker("SPEAKER_00")
    assert (sam.name, sam.name_source) == ("Sam", "voice") and sam.name_confidence > 0.9
    # unknown voice: the heuristic still names it from the transcript (SPEAKER_00 says "Hey Jordan")
    assert (t.speaker("SPEAKER_01").name, t.speaker("SPEAKER_01").name_source) == ("Jordan", "heuristic")
    assert t.speaker("SPEAKER_02").name_source != "voice"  # Jordan's saved voice is from another model

    model, run = voices.read_run_voices(tmp_path / "out")
    assert model == MODEL and set(run) == {"SPEAKER_00", "SPEAKER_01", "SPEAKER_02"}
    assert all(v.source == pipeline.MIX_SOURCE for v in run.values())
    assert np.allclose(run["SPEAKER_01"].centroid, centroids["SPEAKER_01"], atol=1e-5)
    assert events[-1]["voices"] == str(tmp_path / "out" / voices.VOICES_FILE)


def test_pipeline_without_the_voice_library(tmp_path, fake_stages, library):
    audio, _calls, _centroids = fake_stages
    library.save("Sam", SAM, MODEL)
    t, _events = _run(audio, tmp_path / "out", voice_library=False)
    assert t.speaker("SPEAKER_00").name_source != "voice"
    assert (tmp_path / "out" / voices.VOICES_FILE).is_file()  # still written, to save confirmed voices


def test_cached_diarization_keeps_the_voices(tmp_path, fake_stages, library):
    audio, calls, _centroids = fake_stages
    library.save("Sam", SAM, MODEL)
    _run(audio, tmp_path / "a")
    t, events = _run(audio, tmp_path / "b")
    assert calls["diarize"] == 1 and any(e.get("cached") and e.get("stage") == "diarize" for e in events)
    assert t.speaker("SPEAKER_00").name_source == "voice"
    assert set(voices.read_run_voices(tmp_path / "b")[1]) == {"SPEAKER_00", "SPEAKER_01", "SPEAKER_02"}


def test_a_cache_entry_without_voices_is_diarized_again(tmp_path, fake_stages):
    audio, calls, _centroids = fake_stages
    key = cache.diarization_key(
        cache.file_digest(audio), num_speakers=None, min_speakers=None, max_speakers=None, backend="sherpa"
    )
    store = cache.TranscriptCache()
    store.root.mkdir(parents=True)
    (store.root / f"turns-{key}.json").write_text(json.dumps({"turns": [vars(t) for t in TURNS]}))  # old format
    assert store.get_turns(key) == TURNS and store.get_diarization(key) is None
    _run(audio, tmp_path / "out")
    assert calls["diarize"] == 1
    turns, vectors, model = store.get_diarization(key)
    assert turns == TURNS and set(vectors) == {"SPEAKER_00", "SPEAKER_01", "SPEAKER_02"} and model == MODEL


def test_per_track_voices_follow_the_global_speaker_ids(tmp_path, monkeypatch, library):
    """Two group tracks are numbered SPEAKER_00.. across both: each centroid goes with its global id and its
    track as source."""
    paths = {}
    for name in ("mic", "discord", "other"):
        paths[name] = str(tmp_path / f"{name}.wav")
        (tmp_path / f"{name}.wav").write_bytes(name.encode() * 100)
    track_words = {
        paths["mic"]: [Word("Hi", 0.0, 0.3)],
        paths["discord"]: [Word("Hello", 1.0, 1.3), Word("there.", 2.0, 2.3)],
        paths["other"]: [Word("Sup.", 3.0, 3.3)],
    }
    track_turns = {
        paths["discord"]: diarize.DiarizeResult(
            [SpeakerTurn("SPEAKER_00", 0.9, 1.5), SpeakerTurn("SPEAKER_01", 1.9, 2.5)],
            {"SPEAKER_00": heard(SAM, 0.2), "SPEAKER_01": voice(41)},
            [],
            MODEL,
        ),
        paths["other"]: diarize.DiarizeResult(
            [SpeakerTurn("SPEAKER_00", 2.9, 3.5)], {"SPEAKER_00": heard(JORDAN, 0.2)}, [], MODEL
        ),
    }
    monkeypatch.setattr(
        transcribe,
        "transcribe",
        lambda audio_path, **kw: ("en", 4.0, [Word(**vars(w)) for w in track_words[audio_path]]),
    )
    monkeypatch.setattr(diarize, "diarize_detailed", lambda audio_path, **kw: track_turns[audio_path])
    monkeypatch.setattr(pipeline.multitrack, "TrackLevels", _NoLevels)
    library.save("Jordan", JORDAN, MODEL)
    library.save("Anthony", SAM, MODEL)  # the mic person's name: never given to a group speaker
    from better_captions.engine import multitrack

    tracks = [
        multitrack.Track("person", "Anthony", paths["mic"]),
        multitrack.Track("group", "Discord", paths["discord"]),
        multitrack.Track("group", "", paths["other"]),
    ]
    t, _events = _run(None, tmp_path / "out", tracks=tracks, use_cache=False)
    names = {s.id: (s.name, s.name_source) for s in t.speakers}
    assert names["PINNED_00"] == ("Anthony", "user")
    assert names["SPEAKER_02"] == ("Jordan", "voice")  # the second group track's speaker
    assert names["SPEAKER_00"][1] != "voice"  # sounds like the library's "Anthony", who is the mic track
    _model, run = voices.read_run_voices(tmp_path / "out")
    assert {sid: v.source for sid, v in run.items()} == {
        "SPEAKER_00": "Discord",
        "SPEAKER_01": "Discord",
        "SPEAKER_02": "group",
    }
    assert float(run["SPEAKER_02"].centroid @ JORDAN) > 0.9


class _NoLevels:
    def __init__(self, paths):
        pass

    def __call__(self, track, start, end):
        return None

    def close(self):
        pass
