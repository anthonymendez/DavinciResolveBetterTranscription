"""End-to-end: audio file (or one audio file per track) -> transcript.json + one SRT per speaker."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

from better_captions import cues as cues_mod
from better_captions import naming
from better_captions.engine import align, cache, diarize, fixed_tracks, multitrack, transcribe
from better_captions.export import srt
from better_captions.schema import Speaker, Transcript

EventFn = Callable[[dict], None]

# Fraction of total progress each stage covers, in order.
STAGES = [("transcribe", 0.55), ("diarize", 0.30), ("align", 0.03), ("naming", 0.10), ("export", 0.02)]


@dataclass
class RunOptions:
    model: str = "large-v3-turbo"
    language: str | None = None
    device: str = "auto"
    backend: str = "auto"  # transcription backend, see engine.transcribe.BACKENDS
    num_speakers: int | None = None
    min_speakers: int | None = None
    max_speakers: int | None = None
    diarization_backend: str = "sherpa"
    naming_provider: str = "auto"
    naming_options: dict = field(default_factory=dict)
    cue_options: cues_mod.CueOptions = field(default_factory=cues_mod.CueOptions)
    use_cache: bool = True  # reuse words/turns from engine.cache for unchanged audio + settings
    # speakers pinned to their own audio track (--fixed-track NAME=PATH), see engine.fixed_tracks
    fixed_tracks: list[fixed_tracks.FixedTrack] = field(default_factory=list)
    # one audio file per track, each with a role (--track ROLE:NAME=PATH), see engine.multitrack; when set,
    # every speech track is transcribed on its own and the ``audio_path`` given to ``run`` is not used
    tracks: list[multitrack.Track] = field(default_factory=list)


def json_lines_events(event: dict) -> None:
    """Default event sink: one JSON object per stdout line (the Resolve plugin parses these)."""
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


def _stage_progress(emit: EventFn, stage: str) -> Callable[[float], None]:
    offset = 0.0
    for name, weight in STAGES:
        if name == stage:
            break
        offset += weight
    weight = dict(STAGES)[stage]
    return lambda frac, **extra: emit(
        {"event": "progress", "stage": stage, "pct": round(100 * (offset + weight * frac), 1), **extra}
    )


def _try_put(put: Callable, *args) -> None:
    """A cache write failing (disk full, permissions) shouldn't fail the run."""
    try:
        put(*args)
    except OSError as e:
        print(f"better-captions: could not write cache entry: {e}", file=sys.stderr)


def _minus(count: int | None, pinned: int, floor: int) -> int | None:
    return None if count is None else max(floor, count - pinned)


def _diarize(audio_path: str, options: RunOptions, store, p, digest: str | None = None, pinned: int = 0):
    """Speaker turns for ``audio_path`` (cached). With ``pinned`` speakers already taken out of the audio
    (fixed tracks), the speaker counts asked for cover everyone, so that many fewer are left to find."""
    num_speakers = _minus(options.num_speakers, pinned, 1)
    min_speakers = _minus(options.min_speakers, pinned, 0) or None
    max_speakers = _minus(options.max_speakers, pinned, 1)
    if store and digest is None:
        try:
            digest = cache.file_digest(audio_path)
        except OSError:
            digest = None
    turns_key = (
        store
        and digest
        and cache.diarization_key(
            digest,
            num_speakers=num_speakers,
            min_speakers=min_speakers,
            max_speakers=max_speakers,
            backend=options.diarization_backend,
        )
    )
    turns = store.get_turns(turns_key) if turns_key else None
    if turns is not None:
        p(1.0, cached=True)
        return turns
    turns = diarize.diarize(
        audio_path,
        num_speakers=num_speakers,
        min_speakers=min_speakers,
        max_speakers=max_speakers,
        backend=options.diarization_backend,
        progress=p,
    )
    if turns_key:
        _try_put(store.put_turns, turns_key, turns)
    p(1.0)
    return turns


def _transcribe(audio_path: str, options: RunOptions, store, p, digest: str | None = None):
    """(language, duration, words) for ``audio_path`` (cached)."""
    if store and digest is None:
        try:
            digest = cache.file_digest(audio_path)
        except OSError:
            digest = None
    words_key = (
        store
        and digest
        and cache.transcription_key(digest, model=options.model, language=options.language, backend=options.backend)
    )
    hit = store.get_words(words_key) if words_key else None
    if hit:
        p(1.0, cached=True)
        return hit
    language, duration, words = transcribe.transcribe(
        audio_path,
        model=options.model,
        language=options.language,
        device=options.device,
        progress=p,
        backend=options.backend,
    )
    if words_key:
        _try_put(store.put_words, words_key, language, duration, words)
    p(1.0)
    return language, duration, words


def _part(p, index: int, count: int, **extra):
    """Progress of part ``index`` of ``count`` equal parts of a stage (one track of several)."""
    return lambda frac, **more: p((index + frac) / count, **extra, **more)


def _run_tracks(options: RunOptions, emit: EventFn, store):
    """Per-track mode (``options.tracks``): transcribe each speech track alone, diarize only the group tracks,
    merge the words by time and drop bleed. Returns (language, duration, segments, meta)."""
    tracks = options.tracks
    speech = [t for t in tracks if t.speech]
    if not speech:
        raise ValueError("no speech track: every --track is 'none'")
    for t in speech:
        if not os.path.isfile(t.path):
            raise FileNotFoundError(f"audio of track {t.name or t.role!r} not found: {t.path}")

    p = _stage_progress(emit, "transcribe")
    transcribed = []
    for i, t in enumerate(speech):
        label = t.name or t.role
        print(f"better-captions: transcribing track {i + 1}/{len(speech)} ({t.role}: {label})", file=sys.stderr)
        transcribed.append(_transcribe(t.path, options, store, _part(p, i, len(speech), track=label)))
    p(1.0)

    p = _stage_progress(emit, "diarize")
    groups = [i for i, t in enumerate(speech) if t.role == multitrack.GROUP]
    pinned = len(multitrack.person_ids(tracks))
    group_options = options
    if len(groups) > 1:  # the speaker counts cover all group tracks together: only an upper bound per track
        group_options = replace(
            options, num_speakers=None, min_speakers=None, max_speakers=options.max_speakers or options.num_speakers
        )
    turns_of = {}
    for n, i in enumerate(groups):
        if transcribed[i][2]:
            label = speech[i].name or speech[i].role
            turns_of[i] = _diarize(
                speech[i].path, group_options, store, _part(p, n, len(groups), track=label), pinned=pinned
            )
        else:  # nothing said on it: nothing to tell apart
            turns_of[i] = []
    p(1.0)

    p = _stage_progress(emit, "align")
    ids = multitrack.person_ids(tracks)
    numbering = multitrack.GroupNumbering()
    tagged = []
    for i, (t, (_language, _duration, words)) in enumerate(zip(speech, transcribed, strict=True)):
        if t.role == multitrack.PERSON:
            labelled = multitrack.label_person(words, ids[t.name])
        else:
            labelled = numbering.assign(words, turns_of[i])
        tagged.extend((i, w) for w in labelled)
    levels = multitrack.TrackLevels([t.path for t in speech])
    try:
        kept, dropped = multitrack.drop_bleed(tagged, [t.role for t in speech], levels)
    finally:
        levels.close()
    if dropped:
        print(f"better-captions: dropped {dropped} words heard on two tracks (bleed)", file=sys.stderr)
    segments = multitrack.group_segments(multitrack.merge_words(kept))

    counts: dict[str, int] = {}
    for language, _duration, words in transcribed:
        counts[language] = counts.get(language, 0) + len(words)
    language = max(counts, key=counts.get)  # the language most words were heard in
    duration = max(d for _l, d, _w in transcribed)
    meta = {"audio": None, "model": options.model, "tracks": multitrack.describe(tracks), "bleed_dropped": dropped}
    return language, duration, segments, meta


def run(
    audio_path: str | None,
    out_dir: str | Path,
    options: RunOptions | None = None,
    emit: EventFn = json_lines_events,
) -> Transcript:
    options = options or RunOptions()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    if options.tracks:
        store = cache.TranscriptCache() if options.use_cache else None
        language, duration, segments, meta = _run_tracks(options, emit, store)
        pinned = multitrack.person_speakers(options.tracks)
    else:
        language, duration, segments, meta = _run_mix(audio_path, options, emit)
        pinned = fixed_tracks.fixed_speakers(options.fixed_tracks)
    speaker_ids = list(dict.fromkeys(s.speaker for s in segments))
    transcript = Transcript(
        language=language,
        duration=duration,
        speakers=[pinned.get(sid) or Speaker(id=sid) for sid in speaker_ids],
        segments=segments,
        meta=meta,
    )
    p = _stage_progress(emit, "align")
    p(1.0)

    p = _stage_progress(emit, "naming")
    transcript.speakers = naming.name_speakers(transcript, provider=options.naming_provider, **options.naming_options)
    p(1.0)

    p = _stage_progress(emit, "export")
    transcript.cues = cues_mod.build_cues(transcript, options.cue_options)
    transcript_path = out / "transcript.json"
    transcript.save(transcript_path)
    srt_paths = srt.write_speaker_srts(transcript, out)
    p(1.0)

    emit(
        {
            "event": "done",
            "transcript": str(transcript_path),
            "srt": {k: str(v) for k, v in srt_paths.items()},
            "speakers": [{"id": s.id, "name": s.name} for s in transcript.speakers],
        }
    )
    return transcript


def _run_mix(audio_path: str, options: RunOptions, emit: EventFn):
    """The mix of all tracks in one file (with ``--fixed-track`` stems, see engine.fixed_tracks).
    Returns (language, duration, segments, meta)."""
    store, digest = None, None
    if options.use_cache:
        try:
            digest = cache.file_digest(audio_path)
            store = cache.TranscriptCache()
        except OSError:
            pass  # unreadable audio: let the stages report the real error

    p = _stage_progress(emit, "transcribe")
    language, duration, words = _transcribe(audio_path, options, store, p, digest=digest)

    p = _stage_progress(emit, "diarize")
    analysis = None
    if options.fixed_tracks:
        analysis = fixed_tracks.analyze(audio_path, options.fixed_tracks)
        for stem in analysis.stems:
            print(
                f"better-captions: {stem.name} ({stem.path}) talks for {analysis.speech_seconds(stem):.1f} s",
                file=sys.stderr,
            )
        with tempfile.TemporaryDirectory(prefix="better-captions-") as tmp:
            residual = str(Path(tmp) / "residual.wav")
            if analysis.write_residual(residual):
                pinned_count = len(fixed_tracks.speaker_ids(options.fixed_tracks))
                turns = _diarize(residual, options, store, p, pinned=pinned_count)
            else:  # the pinned speakers say everything: nothing left to diarize
                turns = []
                p(1.0)
    else:
        turns = _diarize(audio_path, options, store, p, digest=digest)

    p = _stage_progress(emit, "align")
    if analysis is not None:
        words = fixed_tracks.assign_speakers(analysis, words, turns, align.assign_speakers)
    else:
        words = align.assign_speakers(words, turns)
    segments = align.group_segments(words)
    meta = {"audio": str(audio_path), "model": options.model}
    if options.fixed_tracks:
        ids = fixed_tracks.speaker_ids(options.fixed_tracks)
        meta["fixed_tracks"] = [{"id": ids[t.name], "name": t.name, "audio": t.path} for t in options.fixed_tracks]
    return language, duration, segments, meta
