"""Speakers pinned to their own audio track ("Microphone" = "Anthony").

The plugin renders the usual mix plus each pinned track alone (a "stem") and passes them as
``--fixed-track NAME=PATH``. The mix is still transcribed once; this module decides which words belong
to a pinned speaker and keeps their speech out of diarization:

1. ``analyze`` measures the energy of the mix and of every stem in short frames. A stem *holds* a
   frame when it has voice activity there (energy well above its own noise floor) and, over a short
   window around it, carries a good share of the mix's energy. The share test keeps bleed out: a
   microphone that faintly picks up someone else is active, but that person's own track dominates the
   mix, so the stem's share stays low.
2. ``label_words``: a word mostly inside frames a stem holds is that stem's speaker.
3. ``write_residual``: the mix with every held frame silenced is what gets diarized, so the pinned
   speakers' speech never shapes the clusters of the others.
4. ``merge_turns``: a cluster found in the residual whose speech is mostly a stem's (quiet speech the
   activity test missed) is relabelled to that pinned speaker.
"""

from __future__ import annotations

import wave
from collections.abc import Callable
from dataclasses import dataclass, replace
from itertools import pairwise

import numpy as np

from better_captions.schema import Speaker, SpeakerTurn, Word

SAMPLE_RATE = 16000
FRAME_SECONDS = 0.02
NOISE_PERCENTILE = 10  # a stem's noise floor: this percentile of its frame energies (dB)
ACTIVITY_MARGIN_DB = 12.0  # voice activity: this far above the noise floor...
ACTIVITY_FLOOR_DB = -50.0  # ...and never below this (dBFS; a silent track stays inactive)
MIN_RUN_SECONDS = 0.1  # activity shorter than this is a click, not speech
MAX_GAP_SECONDS = 0.3  # pauses shorter than this inside speech stay active
PAD_SECONDS = 0.1  # word timings are approximate: widen each run of activity by this much
SHARE_WINDOW_SECONDS = 0.5  # the stem's share of the mix energy is measured over this window
MIN_SHARE = 0.35  # a stem holds a frame only when it carries at least this share of the mix energy
WORD_HELD_FRACTION = 0.5  # a word belongs to a stem when the stem holds at least this much of it
MERGE_SHARE = 0.5  # a residual cluster joins a pinned speaker when the stem carries this share of it
ID_PREFIX = "PINNED"
NAME_SOURCE = "user"  # the user typed the name: naming keeps it, the review dialog pre-fills it

LoadFn = Callable[[str], np.ndarray]


@dataclass(frozen=True)
class FixedTrack:
    """One ``--fixed-track NAME=PATH``: everything ``name`` says is on the audio at ``path``."""

    name: str
    path: str


def parse_fixed_track(spec: str) -> FixedTrack:
    """``"NAME=PATH"`` -> FixedTrack (split at the first ``=``, so the path may contain more)."""
    name, sep, path = str(spec).partition("=")
    name, path = name.strip(), path.strip()
    if not sep or not name or not path:
        raise ValueError(f"expected NAME=PATH, got {spec!r}")
    return FixedTrack(name=name, path=path)


def speaker_ids(tracks: list[FixedTrack]) -> dict[str, str]:
    """Pinned speaker name -> its speaker id (PINNED_00, ...); tracks that share a name share a speaker."""
    ids: dict[str, str] = {}
    for track in tracks:
        ids.setdefault(track.name, f"{ID_PREFIX}_{len(ids):02d}")
    return ids


def is_pinned(speaker_id: str | None) -> bool:
    return bool(speaker_id) and speaker_id.startswith(ID_PREFIX + "_")


# --- signal ------------------------------------------------------------------


def frame_energy(samples: np.ndarray, frame: int) -> np.ndarray:
    """Mean square of each ``frame``-sample frame (the last one zero-padded)."""
    x = np.asarray(samples, dtype=np.float64)
    n = -(-len(x) // frame) if len(x) else 0
    padded = np.zeros(n * frame)
    padded[: len(x)] = x
    return (padded.reshape(n, frame) ** 2).mean(axis=1) if n else np.zeros(0)


def _db(energy: np.ndarray) -> np.ndarray:
    return 10.0 * np.log10(energy + 1e-12)


def _runs(mask: np.ndarray) -> list[tuple[int, int]]:
    """[(start, end)) of each run of True."""
    if not len(mask):
        return []
    edges = np.diff(np.concatenate(([0], mask.astype(np.int8), [0])))
    return list(zip(np.flatnonzero(edges == 1), np.flatnonzero(edges == -1), strict=True))


def voice_activity(energy: np.ndarray, frame_seconds: float = FRAME_SECONDS) -> np.ndarray:
    """Frames where a single-speaker track has speech: loud enough over its noise floor, with short
    pauses bridged, clicks dropped and every run padded a little."""
    if not len(energy):
        return np.zeros(0, dtype=bool)
    db = _db(energy)
    threshold = max(ACTIVITY_FLOOR_DB, float(np.percentile(db, NOISE_PERCENTILE)) + ACTIVITY_MARGIN_DB)
    active = db > threshold
    max_gap = round(MAX_GAP_SECONDS / frame_seconds)
    runs = _runs(active)
    for (_s0, e0), (s1, _e1) in pairwise(runs):
        if s1 - e0 <= max_gap:
            active[e0:s1] = True
    min_run = round(MIN_RUN_SECONDS / frame_seconds)
    pad = round(PAD_SECONDS / frame_seconds)
    out = np.zeros_like(active)
    for start, end in _runs(active):
        if end - start >= min_run:
            out[max(0, start - pad) : end + pad] = True
    return out


def _window_sum(x: np.ndarray, width: int) -> np.ndarray:
    return np.convolve(x, np.ones(max(1, width)), mode="same")


def _fit(x: np.ndarray, n: int) -> np.ndarray:
    """``x`` cut or zero-padded to ``n`` samples (a stem render can be a frame longer or shorter)."""
    x = np.asarray(x, dtype=np.float32)
    if len(x) >= n:
        return x[:n]
    return np.concatenate([x, np.zeros(n - len(x), dtype=np.float32)])


# --- analysis ----------------------------------------------------------------


@dataclass
class Stem:
    speaker_id: str
    name: str
    path: str
    energy: np.ndarray  # per frame
    held: np.ndarray  # bool per frame: this stem's speaker is the one talking


class FixedAnalysis:
    """The mix and its pinned stems, framed; see the module docstring."""

    def __init__(self, mix: np.ndarray, stems: list[Stem], sample_rate: int = SAMPLE_RATE):
        self.mix = mix
        self.stems = stems
        self.sample_rate = sample_rate
        self.frame = max(1, round(FRAME_SECONDS * sample_rate))
        self.frame_seconds = self.frame / sample_rate
        self.mix_energy = frame_energy(mix, self.frame)

    @property
    def held(self) -> np.ndarray:
        """Frames any pinned speaker holds."""
        mask = np.zeros(len(self.mix_energy), dtype=bool)
        for stem in self.stems:
            mask |= stem.held
        return mask

    def _span(self, start: float, end: float) -> slice:
        n = len(self.mix_energy)
        lo = min(n, max(0, int(start / self.frame_seconds)))
        hi = min(n, max(lo + 1, int(np.ceil(end / self.frame_seconds))))
        return slice(lo, hi)

    def owner(self, start: float, end: float) -> str | None:
        """The pinned speaker holding most of [start, end), if one holds enough of it."""
        span = self._span(start, end)
        best, best_key = None, (0.0, 0.0)
        for stem in self.stems:
            held = stem.held[span]
            if not len(held):
                continue
            fraction = float(held.mean())
            share = float(stem.energy[span].sum() / (self.mix_energy[span].sum() + 1e-12))
            if fraction >= WORD_HELD_FRACTION and (fraction, share) > best_key:
                best, best_key = stem.speaker_id, (fraction, share)
        return best

    def label_words(self, words: list[Word]) -> list[Word]:
        """Copies of ``words``: a pinned speaker's words get its id, the others ``speaker=None``."""
        return [replace(w, speaker=self.owner(w.start, w.end)) for w in words]

    def residual(self) -> np.ndarray:
        """The mix with every frame a pinned speaker holds silenced."""
        out = self.mix.copy()
        for start, end in _runs(self.held):
            out[start * self.frame : end * self.frame] = 0.0
        return out

    def write_residual(self, path: str) -> bool:
        """Write ``residual`` as a 16-bit mono WAV; False (nothing written) when it is all silence."""
        samples = self.residual()
        if not len(samples) or not np.any(np.abs(samples) > 1e-4):
            return False
        write_wav(path, samples, self.sample_rate)
        return True

    def merge_turns(self, turns: list[SpeakerTurn]) -> list[SpeakerTurn]:
        """Relabel each residual cluster whose speech is mostly one stem's to that pinned speaker."""
        merged: dict[str, str] = {}
        for label in dict.fromkeys(t.speaker for t in turns):
            mix_total = 0.0
            stem_totals = [0.0] * len(self.stems)
            for t in turns:
                if t.speaker != label:
                    continue
                span = self._span(t.start, t.end)
                mix_total += float(self.mix_energy[span].sum())
                for i, stem in enumerate(self.stems):
                    stem_totals[i] += float(stem.energy[span].sum())
            if not self.stems or mix_total <= 0:
                continue
            i = int(np.argmax(stem_totals))
            if stem_totals[i] / mix_total >= MERGE_SHARE:
                merged[label] = self.stems[i].speaker_id
        return [replace(t, speaker=merged.get(t.speaker, t.speaker)) for t in turns]

    def speech_seconds(self, stem: Stem) -> float:
        return float(stem.held.sum()) * self.frame_seconds


def analyze(
    mix_path: str, tracks: list[FixedTrack], load: LoadFn | None = None, sample_rate: int = SAMPLE_RATE
) -> FixedAnalysis:
    """Load the mix and each pinned stem (mono, ``sample_rate``) and find where each stem's speaker talks."""
    if load is None:
        from better_captions.engine import diarize

        def load(path):
            return diarize.load_audio(path, sample_rate)

    mix = np.asarray(load(mix_path), dtype=np.float32)
    frame = max(1, round(FRAME_SECONDS * sample_rate))
    frame_seconds = frame / sample_rate
    mix_energy = frame_energy(mix, frame)
    window = round(SHARE_WINDOW_SECONDS / frame_seconds)
    mix_window = _window_sum(mix_energy, window)
    ids = speaker_ids(tracks)
    stems = []
    for track in tracks:
        samples = _fit(load(track.path), len(mix))
        energy = frame_energy(samples, frame)
        share = _window_sum(energy, window) / (mix_window + 1e-12)
        held = voice_activity(energy, frame_seconds) & (share >= MIN_SHARE)
        stems.append(Stem(speaker_id=ids[track.name], name=track.name, path=track.path, energy=energy, held=held))
    return FixedAnalysis(mix, stems, sample_rate)


def assign_speakers(
    analysis: FixedAnalysis, words: list[Word], turns: list[SpeakerTurn], assign: Callable
) -> list[Word]:
    """Pinned words keep their pinned speaker; ``assign(rest, turns)`` (align.assign_speakers) labels the rest
    from the residual's turns, after clusters that are really a pinned speaker were merged into it."""
    labelled = analysis.label_words(words)
    rest_index = [i for i, w in enumerate(labelled) if w.speaker is None]
    rest = assign([labelled[i] for i in rest_index], analysis.merge_turns(turns)) if rest_index else []
    for i, word in zip(rest_index, rest, strict=True):
        labelled[i] = word
    return labelled


def fixed_speakers(tracks: list[FixedTrack]) -> dict[str, Speaker]:
    """Speaker id -> the named Speaker, as transcript.json carries it."""
    return {
        sid: Speaker(id=sid, name=name, name_confidence=1.0, name_source=NAME_SOURCE)
        for name, sid in speaker_ids(tracks).items()
    }


def write_wav(path: str, samples: np.ndarray, sample_rate: int = SAMPLE_RATE) -> None:
    """Mono 16-bit PCM WAV (stdlib ``wave``)."""
    pcm = (np.clip(np.asarray(samples, dtype=np.float64), -1.0, 1.0) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm.tobytes())
