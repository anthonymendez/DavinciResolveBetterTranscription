"""End-to-end: audio file -> transcript.json + one SRT per speaker."""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from better_captions import cues as cues_mod
from better_captions import naming
from better_captions.engine import align, cache, diarize, transcribe
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


def run(
    audio_path: str, out_dir: str | Path, options: RunOptions | None = None, emit: EventFn = json_lines_events
) -> Transcript:
    options = options or RunOptions()
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)

    store, digest = None, None
    if options.use_cache:
        try:
            digest = cache.file_digest(audio_path)
            store = cache.TranscriptCache()
        except OSError:
            pass  # unreadable audio: let the stages report the real error

    p = _stage_progress(emit, "transcribe")
    words_key = store and cache.transcription_key(
        digest, model=options.model, language=options.language, backend=options.backend
    )
    hit = store.get_words(words_key) if store else None
    if hit:
        language, duration, words = hit
        p(1.0, cached=True)
    else:
        language, duration, words = transcribe.transcribe(
            audio_path,
            model=options.model,
            language=options.language,
            device=options.device,
            progress=p,
            backend=options.backend,
        )
        if store:
            _try_put(store.put_words, words_key, language, duration, words)
        p(1.0)

    p = _stage_progress(emit, "diarize")
    turns_key = store and cache.diarization_key(
        digest,
        num_speakers=options.num_speakers,
        min_speakers=options.min_speakers,
        max_speakers=options.max_speakers,
        backend=options.diarization_backend,
    )
    turns = store.get_turns(turns_key) if store else None
    if turns is not None:
        p(1.0, cached=True)
    else:
        turns = diarize.diarize(
            audio_path,
            num_speakers=options.num_speakers,
            min_speakers=options.min_speakers,
            max_speakers=options.max_speakers,
            backend=options.diarization_backend,
            progress=p,
        )
        if store:
            _try_put(store.put_turns, turns_key, turns)
        p(1.0)

    p = _stage_progress(emit, "align")
    words = align.assign_speakers(words, turns)
    segments = align.group_segments(words)
    speaker_ids = list(dict.fromkeys(s.speaker for s in segments))
    transcript = Transcript(
        language=language,
        duration=duration,
        speakers=[Speaker(id=sid) for sid in speaker_ids],
        segments=segments,
        meta={"audio": str(audio_path), "model": options.model},
    )
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
