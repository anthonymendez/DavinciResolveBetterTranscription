"""engine.cache on its own, and pipeline.run reusing cached words/turns."""

import hashlib
import json
import os

import pytest

from better_captions import pipeline
from better_captions.engine import cache, diarize, transcribe
from better_captions.pipeline import RunOptions
from better_captions.schema import SpeakerTurn, Word

WORDS = [Word("Hey", 0.0, 0.25), Word("Jordan,", 0.3, 0.55), Word("Good,", 2.0, 2.25), Word("Maria.", 2.6, 2.85)]
TURNS = [SpeakerTurn("SPEAKER_00", 0.0, 1.5), SpeakerTurn("SPEAKER_01", 1.9, 3.0)]


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("BETTER_CAPTIONS_HOME", str(tmp_path / "home"))
    return tmp_path / "home"


@pytest.fixture
def calls(monkeypatch):
    """Fake the heavy stages and count how often each actually runs."""
    counts = {"transcribe": 0, "diarize": 0}

    def fake_transcribe(audio_path, **kw):
        counts["transcribe"] += 1
        return "en", 3.5, [Word(w.text, w.start, w.end) for w in WORDS]

    def fake_diarize(audio_path, **kw):
        counts["diarize"] += 1
        return diarize.DiarizeResult(list(TURNS))

    monkeypatch.setattr(transcribe, "transcribe", fake_transcribe)
    monkeypatch.setattr(diarize, "diarize_detailed", fake_diarize)
    return counts


@pytest.fixture
def audio(tmp_path):
    path = tmp_path / "in.wav"
    path.write_bytes(b"RIFF fake audio " * 1000)
    return path


def run(audio, tmp_path, **opts):
    events = []
    options = RunOptions(naming_provider="heuristic", **opts)
    transcript = pipeline.run(str(audio), tmp_path / "out", options, emit=events.append)
    return transcript, events


def cached_stages(events):
    return {e["stage"] for e in events if e["event"] == "progress" and e.get("cached")}


# --- keys -------------------------------------------------------------------


def test_file_digest_is_sha256_of_content(audio):
    assert cache.file_digest(audio) == hashlib.sha256(audio.read_bytes()).hexdigest()


def test_keys_depend_on_their_own_settings_only():
    d = "ab" * 32
    base = cache.transcription_key(d, model="m", language=None, backend="auto")
    assert base == cache.transcription_key(d, model="m", language=None, backend="auto")
    assert base != cache.transcription_key(d, model="m2", language=None, backend="auto")
    assert base != cache.transcription_key(d, model="m", language="en", backend="auto")
    assert base != cache.transcription_key(d, model="m", language=None, backend="mlx")
    assert base != cache.transcription_key("cd" * 32, model="m", language=None, backend="auto")

    kw = dict(num_speakers=None, min_speakers=None, max_speakers=None, backend="sherpa")
    turns = cache.diarization_key(d, **kw)
    assert turns == cache.diarization_key(d, **kw)
    for change in ({"num_speakers": 2}, {"min_speakers": 1}, {"max_speakers": 4}, {"backend": "pyannote"}):
        assert turns != cache.diarization_key(d, **{**kw, **change})


# --- store ------------------------------------------------------------------


def test_default_root_is_under_data_dir(home):
    assert cache.TranscriptCache().root == home / "cache"


def test_words_and_turns_round_trip(tmp_path):
    store = cache.TranscriptCache(tmp_path / "c")
    assert store.get_words("k") is None and store.get_turns("k") is None
    store.put_words("k", "en", 3.5, WORDS)
    store.put_turns("k", TURNS)
    assert store.get_words("k") == ("en", 3.5, WORDS)
    assert store.get_turns("k") == TURNS


@pytest.mark.parametrize("content", ["{not json", "[1, 2]", '{"language": "en"}', '{"words": [{"bogus": 1}]}'])
def test_corrupted_entry_is_a_miss_and_removed(tmp_path, content):
    store = cache.TranscriptCache(tmp_path / "c")
    store.put_words("k", "en", 3.5, WORDS)
    path = store._path("words", "k")
    path.write_text(content, encoding="utf-8")
    assert store.get_words("k") is None
    assert not path.exists()


def test_eviction_drops_least_recently_used(tmp_path):
    store = cache.TranscriptCache(tmp_path / "c")
    for i, key in enumerate(["a", "b", "c"]):
        store.put_turns(key, TURNS)
        os.utime(store._path("turns", key), (1000 + i, 1000 + i))
    size = store._path("turns", "a").stat().st_size
    store.get_turns("a")  # "a" becomes the most recently used; "b" is now the oldest
    store.max_bytes = 3 * size  # room for exactly three entries
    store.put_turns("d", TURNS)
    assert store.get_turns("b") is None
    assert all(store.get_turns(k) == TURNS for k in ["a", "c", "d"])


def test_default_cap_is_2gb():
    assert cache.TranscriptCache().max_bytes == 2 * 1024**3 == cache.DEFAULT_MAX_BYTES


def test_clear(tmp_path, home):
    assert cache.clear() == 0  # no cache dir yet
    store = cache.TranscriptCache()
    store.put_words("k", "en", 3.5, WORDS)
    store.put_turns("k", TURNS)
    assert cache.clear() == 2
    assert store.get_words("k") is None and store.get_turns("k") is None


# --- pipeline ---------------------------------------------------------------


def test_second_run_hits_both_stages(audio, tmp_path, calls):
    first, events = run(audio, tmp_path)
    assert calls == {"transcribe": 1, "diarize": 1}
    assert cached_stages(events) == set()

    second, events = run(audio, tmp_path)
    assert calls == {"transcribe": 1, "diarize": 1}
    assert cached_stages(events) == {"transcribe", "diarize"}
    hit = next(e for e in events if e.get("cached") and e["stage"] == "transcribe")
    assert hit == {"event": "progress", "stage": "transcribe", "pct": 55.0, "cached": True}
    assert second.segments == first.segments
    assert [s.name for s in second.speakers] == [s.name for s in first.speakers]
    assert events[-1]["event"] == "done"  # naming/cues/export still ran


def test_speaker_count_change_reruns_only_diarization(audio, tmp_path, calls):
    run(audio, tmp_path)
    _, events = run(audio, tmp_path, num_speakers=2)
    assert calls == {"transcribe": 1, "diarize": 2}
    assert cached_stages(events) == {"transcribe"}


def test_model_change_reruns_only_transcription(audio, tmp_path, calls):
    run(audio, tmp_path)
    _, events = run(audio, tmp_path, model="small")
    assert calls == {"transcribe": 2, "diarize": 1}
    assert cached_stages(events) == {"diarize"}


def test_changed_audio_is_a_miss(audio, tmp_path, calls):
    run(audio, tmp_path)
    audio.write_bytes(b"different audio")
    _, events = run(audio, tmp_path)
    assert calls == {"transcribe": 2, "diarize": 2}
    assert cached_stages(events) == set()


def test_corrupted_cache_file_reruns_stage(audio, tmp_path, calls, home):
    run(audio, tmp_path)
    for p in (home / "cache").glob("words-*.json"):
        p.write_text("garbage", encoding="utf-8")
    _, events = run(audio, tmp_path)
    assert calls == {"transcribe": 2, "diarize": 1}
    assert cached_stages(events) == {"diarize"}


def test_use_cache_false_neither_reads_nor_writes(audio, tmp_path, calls, home):
    run(audio, tmp_path, use_cache=False)
    assert not (home / "cache").exists()
    run(audio, tmp_path)
    _, events = run(audio, tmp_path, use_cache=False)
    assert calls == {"transcribe": 3, "diarize": 3}
    assert cached_stages(events) == set()


def test_cache_write_failure_does_not_fail_run(audio, tmp_path, calls, monkeypatch):
    def boom(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(cache.TranscriptCache, "_write", boom)
    _, events = run(audio, tmp_path)
    assert events[-1]["event"] == "done"


def test_cli_no_cache_flag_and_clear(monkeypatch, capsys):
    from better_captions import cli
    from better_captions.engine import cache as cache_mod

    seen = {}
    monkeypatch.setattr(cli, "run", lambda audio, out, options: seen.setdefault("use_cache", options.use_cache))
    assert cli.main(["run", "a.wav", "--out", "o", "--no-cache"]) == 0
    assert seen["use_cache"] is False

    monkeypatch.setattr(cache_mod, "clear", lambda root=None: 3)
    assert cli.main(["cache", "clear"]) == 0
    assert '"removed": 3' in capsys.readouterr().out


def test_cli_verbose_logs_and_tracebacks_go_to_stderr(monkeypatch, capsys):
    """`run --verbose` (the plugin's debug mode): engine loggers at DEBUG, tracebacks on stderr, stdout stays JSON."""
    import logging

    from better_captions import cli

    root, engine = logging.getLogger(), logging.getLogger("better_captions")
    saved = (root.handlers[:], root.level, engine.level)

    def boom(audio, out, options):
        logging.getLogger("better_captions.test").debug("loading the model")
        raise RuntimeError("no model")

    monkeypatch.setattr(cli, "run", boom)
    try:
        assert cli.main(["run", "a.wav", "--out", "o", "--verbose"]) == 1
    finally:
        root.handlers[:] = saved[0]
        root.setLevel(saved[1])
        engine.setLevel(saved[2])
    captured = capsys.readouterr()
    assert [json.loads(line)["event"] for line in captured.out.splitlines()] == ["error"]
    assert "DEBUG better_captions.test: loading the model" in captured.err
    assert "Traceback (most recent call last)" in captured.err and "RuntimeError: no model" in captured.err
