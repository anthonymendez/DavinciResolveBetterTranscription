import os

import pytest

from better_captions_plugin import render_cache
from better_captions_plugin.timing import TimelineClock


class FakePoolItem:
    def __init__(self, path, media_id):
        self.path = path
        self.media_id = media_id

    def GetMediaId(self):
        return self.media_id

    def GetClipProperty(self, key=None):
        return self.path if key == "File Path" else {"File Path": self.path}


class FakeClip:
    def __init__(self, path="a.wav", start=0, end=100, left=0, right=0, volume=0.0, pan=0.0, media_id="m1"):
        self.pool_item = FakePoolItem(path, media_id)
        self.start, self.end, self.left, self.right = start, end, left, right
        self.props = {"Volume": volume, "Pan": pan, "Opacity": 100.0, "ZoomX": 1.0}

    def GetName(self):
        return "clip"

    def GetMediaPoolItem(self):
        return self.pool_item

    def GetStart(self):
        return self.start

    def GetEnd(self):
        return self.end

    def GetLeftOffset(self):
        return self.left

    def GetRightOffset(self):
        return self.right

    def GetProperty(self, key=None):
        return dict(self.props)


class FakeRenderTimeline:
    def __init__(self, tracks=None, uid="tl-1", name="Main"):
        self.tracks = tracks if tracks is not None else [[FakeClip(), FakeClip("b.wav", 100, 200)], [FakeClip("c.wav")]]
        self.enabled = [True] * len(self.tracks)
        self.uid, self.name = uid, name

    def GetUniqueId(self):
        return self.uid

    def GetName(self):
        return self.name

    def GetTrackCount(self, track_type):
        return len(self.tracks) if track_type == "audio" else 0

    def GetIsTrackEnabled(self, track_type, index):
        return self.enabled[index - 1]

    def GetItemListInTrack(self, track_type, index):
        return self.tracks[index - 1]


CLOCK = TimelineClock(fps=24.0, start_frame=86400)


def fp(timeline, start=86400, end=None, clock=CLOCK):
    return render_cache.fingerprint(timeline, start, end, clock)


def test_unchanged_timeline_gives_the_same_fingerprint():
    assert fp(FakeRenderTimeline()) == fp(FakeRenderTimeline())


@pytest.mark.parametrize(
    "change",
    [
        lambda tl: setattr(tl.tracks[0][0], "start", 5),
        lambda tl: setattr(tl.tracks[0][1], "end", 250),
        lambda tl: setattr(tl.tracks[0][0], "left", 12),
        lambda tl: setattr(tl.tracks[0][0], "right", 12),
        lambda tl: setattr(tl.tracks[0][0].pool_item, "path", "other.wav"),
        lambda tl: setattr(tl.tracks[0][0].pool_item, "media_id", "m2"),
        lambda tl: tl.tracks[0][0].props.update(Volume=-6.0),
        lambda tl: tl.tracks[0][0].props.update(Pan=0.5),
        lambda tl: tl.tracks[1].append(FakeClip("d.wav")),
        lambda tl: tl.tracks[0].pop(),
        lambda tl: setattr(tl, "name", "Renamed"),
        lambda tl: setattr(tl, "uid", "tl-2"),
    ],
)
def test_fingerprint_changes_when_the_audio_does(change):
    timeline = FakeRenderTimeline()
    before = fp(timeline)
    change(timeline)
    assert fp(timeline) != before


def test_fingerprint_ignores_video_only_properties():
    timeline = FakeRenderTimeline()
    before = fp(timeline)
    timeline.tracks[0][0].props["ZoomX"] = 2.0
    assert fp(timeline) == before


def test_fingerprint_changes_with_the_range():
    timeline = FakeRenderTimeline()
    assert fp(timeline, 86400, 86500) != fp(timeline, 86400, None)
    assert fp(timeline, 86400, 86500) != fp(timeline, 86410, 86500)
    assert fp(timeline, 86400, 86500) != fp(timeline, 86400, 86600)


def test_fingerprint_changes_with_the_frame_rate():
    timeline = FakeRenderTimeline()
    assert fp(timeline, clock=TimelineClock(fps=25.0, start_frame=86400)) != fp(timeline)


def test_fingerprint_changes_when_a_track_is_enabled_or_disabled():
    timeline = FakeRenderTimeline()
    before = fp(timeline)
    timeline.enabled[1] = False
    muted = fp(timeline)
    assert muted != before
    timeline.enabled[1] = True
    assert fp(timeline) == before


def test_fingerprint_survives_an_api_that_reports_nothing():
    class Bare:
        pass

    assert fp(Bare()) == fp(Bare())


def _wav(tmp_path, name, data=b"RIFF"):
    path = tmp_path / name
    path.write_bytes(data)
    return str(path)


def _fp(n):
    return "%064x" % n


def test_lookup_misses_before_anything_is_stored(tmp_path):
    assert render_cache.lookup(str(tmp_path / "cache"), _fp(1)) is None
    (tmp_path / "cache").mkdir()
    assert render_cache.lookup(str(tmp_path / "cache"), _fp(1)) is None


def test_store_then_lookup_round_trip(tmp_path):
    cache = str(tmp_path / "cache")
    source = _wav(tmp_path, "render.wav", b"audio bytes")
    cached = render_cache.store(cache, _fp(1), source)
    assert os.path.dirname(cached) == cache
    assert render_cache.lookup(cache, _fp(1)) == cached
    with open(cached, "rb") as handle:
        assert handle.read() == b"audio bytes"
    assert render_cache.lookup(cache, _fp(2)) is None
    assert os.path.exists(source)  # the caller's own file is left alone


def test_store_replaces_an_existing_entry(tmp_path):
    cache = str(tmp_path / "cache")
    render_cache.store(cache, _fp(1), _wav(tmp_path, "one.wav", b"old"))
    cached = render_cache.store(cache, _fp(1), _wav(tmp_path, "two.wav", b"new"))
    with open(render_cache.lookup(cache, _fp(1)), "rb") as handle:
        assert handle.read() == b"new"
    assert os.listdir(cache) == [os.path.basename(cached)]


def test_store_prunes_to_the_newest_entries(tmp_path):
    cache = str(tmp_path / "cache")
    source = _wav(tmp_path, "render.wav")
    for n in range(1, 4):
        path = render_cache.store(cache, _fp(n), source, keep=2)
        os.utime(path, (1000 + n, 1000 + n))  # distinct ages: 1 oldest
    assert render_cache.lookup(cache, _fp(1)) is None
    assert render_cache.lookup(cache, _fp(2)) is not None
    assert render_cache.lookup(cache, _fp(3)) is not None


def test_a_lookup_hit_protects_an_entry_from_pruning(tmp_path):
    cache = str(tmp_path / "cache")
    source = _wav(tmp_path, "render.wav")
    for n in (1, 2):
        os.utime(render_cache.store(cache, _fp(n), source, keep=2), (1000 + n, 1000 + n))
    assert render_cache.lookup(cache, _fp(1))  # touched: now the most recently used
    render_cache.store(cache, _fp(3), source, keep=2)
    assert render_cache.lookup(cache, _fp(1)) is not None
    assert render_cache.lookup(cache, _fp(2)) is None


def test_default_keeps_a_few_entries(tmp_path):
    cache = str(tmp_path / "cache")
    source = _wav(tmp_path, "render.wav")
    for n in range(1, render_cache.KEEP_ENTRIES + 4):
        os.utime(render_cache.store(cache, _fp(n), source), (1000 + n, 1000 + n))
    assert len(os.listdir(cache)) == render_cache.KEEP_ENTRIES


def test_store_prunes_to_the_size_budget(tmp_path):
    cache = str(tmp_path / "cache")
    source = _wav(tmp_path, "render.wav", b"x" * 100)
    for n in range(1, 5):
        os.utime(render_cache.store(cache, _fp(n), source, max_bytes=250), (1000 + n, 1000 + n))
    assert [render_cache.lookup(cache, _fp(n)) is not None for n in range(1, 5)] == [False, False, True, True]


def test_the_entry_just_stored_is_kept_even_over_the_budget(tmp_path):
    cache = str(tmp_path / "cache")
    render_cache.store(cache, _fp(1), _wav(tmp_path, "small.wav", b"x"), max_bytes=50)
    render_cache.store(cache, _fp(2), _wav(tmp_path, "big.wav", b"x" * 100), max_bytes=50)
    assert render_cache.lookup(cache, _fp(2)) is not None
    assert render_cache.lookup(cache, _fp(1)) is None


def test_runs_on_other_timelines_do_not_evict_an_unchanged_timelines_renders(tmp_path):
    # Studio 21.1.2: a two-track run on the 60 fps timeline, then two runs each on the 23.976 and
    # 29.97 DF copies and one more, then the 60 fps timeline again with nothing changed. With five
    # entries kept, its renders were gone and both tracks rendered again.
    cache = str(tmp_path / "cache")
    source = _wav(tmp_path, "render.wav", b"x" * 1000)
    clock = 1000
    for n in (1, 2, 10, 11, 12, 13, 14):
        clock += 1
        os.utime(render_cache.store(cache, _fp(n), source), (clock, clock))
    assert render_cache.lookup(cache, _fp(1)) is not None
    assert render_cache.lookup(cache, _fp(2)) is not None


def test_an_empty_cached_file_is_not_a_hit(tmp_path):
    cache = str(tmp_path / "cache")
    cached = render_cache.store(cache, _fp(1), _wav(tmp_path, "render.wav"))
    open(cached, "wb").close()
    assert render_cache.lookup(cache, _fp(1)) is None


def test_a_real_fingerprint_is_a_valid_key(tmp_path):
    cache = str(tmp_path / "cache")
    key = fp(FakeRenderTimeline())
    cached = render_cache.store(cache, key, _wav(tmp_path, "render.wav"))
    assert render_cache.lookup(cache, key) == cached


@pytest.mark.parametrize("bad", ["", "../x", "abc", None])
def test_store_rejects_keys_that_are_not_fingerprints(tmp_path, bad):
    with pytest.raises(ValueError):
        render_cache.store(str(tmp_path / "cache"), bad, _wav(tmp_path, "render.wav"))
    assert render_cache.lookup(str(tmp_path), bad) is None
