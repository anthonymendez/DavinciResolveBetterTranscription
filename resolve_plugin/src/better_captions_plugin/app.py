"""The Better Captions flow, from menu click to new tracks.

1. read ``<data>/config.json``
2. render the current timeline's audio (or its in/out range) to a temp WAV
3. run the engine subprocess, streaming its progress into a dialog
4. load ``transcript.json``
5. review dialog: rename speakers, pick outputs and a Text+ template per speaker; "Fix
   speakers…" merges / reassigns / splits speakers (``speaker_edits``), saved as
   ``edits.json`` next to ``transcript.json`` and in ``<data>/edits/<audio sha256>.json`` so a
   re-run on the same audio re-applies them
6. create new tracks (subtitle and/or Text+) from the edited transcript, never touching existing ones;
   with "Highlight words as they're spoken" each Text+ cue becomes one clip per word step
   (``word_highlight``); the toggle and colour, and the speaker-naming provider picked for the
   next run, are remembered in config.json
7. summary; the temp dir is kept only when the user still needs the SRT files

The progress window lists these steps (``STEP_LABELS``) with done / current / pending markers. Its
Pause button holds the flow before the next step (``hold_while_paused``) and suspends the engine
process; Cancel still works while paused.
"""

from __future__ import annotations

import os
import shutil
import tempfile
import time
import traceback

from better_captions_plugin import (
    compat,
    debug_log,
    engine_runner,
    planning,
    resolve_io,
    speaker_edits,
    word_highlight,
)
from better_captions_plugin import config as config_mod
from better_captions_plugin import transcript as transcript_mod
from better_captions_plugin.ui import (
    NAMING_CHOICES,
    Backend,
    ReviewRequest,
    ReviewResult,
    SpeakerRow,
    choose_backend,
    naming_index,
    speaker_rows,
)

TITLE = "Better Captions"

# The steps shown in the progress window, in order; the engine's stages sit between rendering and loading.
STEP_RENDER = 0
STEP_ENGINE = 1  # the first engine stage
STEP_LOAD = STEP_ENGINE + len(engine_runner.STAGES)
STEP_REVIEW = STEP_LOAD + 1
STEP_TRACKS = STEP_REVIEW + 1
STEP_LABELS = (
    ["Render audio"]
    + [engine_runner.stage_label(stage) for stage in engine_runner.STAGES]
    + ["Load transcript", "Review speakers", "Create tracks"]
)
PAUSE_POLL = 0.05  # seconds between repaints while the flow is held


class Cancelled(Exception):
    pass


def hold_while_paused(progress, sleep=None):
    """Wait (repainting the window) while the user has the flow paused, before it moves on to its next step.
    Cancelling ends the wait and the run."""
    if not progress.paused:
        return
    progress.log("Paused. Press Resume to continue.")
    sleep = sleep or time.sleep
    while progress.paused and not progress.cancelled:
        progress.pump()
        sleep(PAUSE_POLL)
    if progress.cancelled:
        raise Cancelled()
    progress.log("Resumed.")


def start_step(progress, step: int, sleep=None):
    """Move the progress window on to ``step``, once a pause (if any) is over."""
    hold_while_paused(progress, sleep)
    progress.set_step(step)


class Outcome:
    """What happened, for the summary dialog and for tests."""

    def __init__(self):
        self.created = []  # resolve_io.TrackResult
        self.subtitle_fallbacks = []  # (TrackPlan, message)
        self.warnings = []
        self.work_dir = None
        self.kept_work_dir = False
        self.cancelled = False
        self.error = None

    def summary(self) -> str:
        lines = []
        for result in self.created:
            kind = "subtitle track" if result.track_type == "subtitle" else "Text+ track"
            lines.append(
                'Created %s %d "%s" with %d clips.' % (kind, result.track_index, result.plan.name, result.placed)
            )
            for note in result.misplaced[:5]:
                lines.append("    note: " + note)
            if len(result.misplaced) > 5:
                lines.append("    ... and %d more placement notes" % (len(result.misplaced) - 5))
            for note in result.text_failures[:5]:
                lines.append("    text not set: " + note)
            if result.highlighted:
                lines.append("    spoken word highlighted in %d clips" % result.highlighted)
            for note in result.highlight_failures[:5]:
                lines.append("    not highlighted: " + note)
            if len(result.highlight_failures) > 5:
                lines.append("    ... and %d more clips not highlighted" % (len(result.highlight_failures) - 5))
            for note in result.notes:
                lines.append("    note: " + note)
        if self.subtitle_fallbacks:
            lines.append("")
            lines.append("Resolve's API could not place these SRT files on a subtitle track; nothing was left behind.")
            lines.append("Import them by hand: File > Import > Subtitle, then drag the subtitle onto the timeline")
            lines.append("(or right-click it > Insert Selected Subtitles to Timeline Using Timecode).")
            for plan, message in self.subtitle_fallbacks:
                lines.append("  %s: %s" % (plan.name, plan.srt_path))
                lines.append("      (%s)" % message)
        for warning in self.warnings:
            lines.append("Warning: " + warning)
        if self.kept_work_dir and self.work_dir:
            lines.append("")
            lines.append("Files kept in %s" % self.work_dir)
        return "\n".join(lines) or "Nothing was created."


def main(
    resolve=None,
    fusion=None,
    bmd=None,
    backend: Backend | None = None,
    config: dict | None = None,
    work_dir: str | None = None,
    environ=None,
    popen=None,
    sleep=None,
) -> Outcome:
    """Entry point used by the launcher script (and by tests with fakes injected)."""
    outcome = Outcome()
    owns_backend = backend is None
    try:
        if config is None:
            config = config_mod.load_config(environ=environ)
        # Debug log (config "debug" or BETTER_CAPTIONS_DEBUG=1): trace every Resolve API call.
        debug = debug_log.start(config, environ)
        if debug is not None:
            resolve = debug.wrap(resolve)
        # Free Resolve 21.1+ gives Python scripts no usable API (compat.py): say so before any
        # UI is set up (UIManager is Studio-only anyway) and before touching the project.
        info = compat.detect(resolve)
        debug_log.note("Resolve: %s (studio: %s, version: %r)" % (info.label, info.studio, info.version))
        if resolve is None or info.python_gated:
            outcome.error = compat.NO_RESOLVE_MESSAGE if resolve is None else compat.gated_message(info)
            if backend is None and (fusion is not None or bmd is not None):
                backend = _message_backend()  # inside Resolve's script host: a dialog, not just the Console
            _print_or_show(backend, debug_log.with_path(outcome.error))
            return outcome
        outcome.warnings.extend(compat.warnings_for(info))
        if backend is None:
            backend = choose_backend(resolve, fusion, bmd, preference=str(config.get("ui", "auto")), studio=info.studio)
        debug_log.note("UI backend: %s" % type(backend).__name__)
        try:
            run(resolve, backend, config, outcome, work_dir=work_dir, popen=popen, sleep=sleep, info=info)
        except Cancelled:
            outcome.cancelled = True
        except (
            config_mod.ConfigError,
            resolve_io.ResolveError,
            engine_runner.EngineError,
            transcript_mod.TranscriptError,
        ) as exc:
            outcome.error = str(exc)
            debug_log.exception("Error:")
            backend.error(TITLE, debug_log.with_path(str(exc)))
        except Exception as exc:  # anything else: show it with a traceback rather than dying silently
            outcome.error = "%s: %s" % (type(exc).__name__, exc)
            debug_log.exception("Unexpected error:")
            backend.error(TITLE, debug_log.with_path("Unexpected error: %s\n\n%s" % (exc, traceback.format_exc())))
    except config_mod.ConfigError as exc:
        outcome.error = str(exc)
        debug_log.start(config or {}, environ)  # BETTER_CAPTIONS_DEBUG=1 still logs a missing config
        debug_log.exception("Config error:")
        _print_or_show(backend, debug_log.with_path(str(exc)))
    except BaseException:
        debug_log.exception("Fatal error:")
        raise
    finally:
        _cleanup(outcome)
        if owns_backend and backend is not None:
            backend.close()
        debug_log.finish(outcome)
    return outcome


def run(resolve, backend: Backend, config: dict, outcome: Outcome, work_dir=None, popen=None, sleep=None, info=None):
    info = info or compat.detect(resolve)
    project, timeline = resolve_io.current_project_and_timeline(resolve, info=info)
    media_pool = project.GetMediaPool()
    clock = resolve_io.timeline_clock(timeline, project)
    range_start, range_end = resolve_io.render_range(timeline, clock)
    timeline_name = _safe(timeline.GetName, "timeline")

    outcome.work_dir = work_dir or tempfile.mkdtemp(prefix="better-captions-")
    audio_dir = os.path.join(outcome.work_dir, "audio")
    out_dir = os.path.join(outcome.work_dir, "out")

    progress = backend.progress("%s – %s" % (TITLE, timeline_name))
    try:
        progress.log("Resolve: " + info.label)
        progress.set_steps(STEP_LABELS)
        progress.set_step(STEP_RENDER)
        progress.set_status("Rendering timeline audio…")
        progress.set_progress(None)
        render_kwargs = {"pump": progress.pump, "should_cancel": lambda: progress.cancelled}
        if sleep is not None:
            render_kwargs["sleep"] = sleep
        debug_log.render_diagnostics(project)
        try:
            wav = resolve_io.render_audio(project, timeline, audio_dir, range_start, range_end, **render_kwargs)
        except resolve_io.ResolveError as exc:
            if progress.cancelled:
                raise Cancelled() from exc
            raise exc
        progress.log("Rendered audio: %s" % wav)

        argv = config_mod.engine_command(config, wav, out_dir)
        if debug_log.current() is not None:
            argv.append("--verbose")  # the engine's debug logging, on stderr
        start_step(progress, STEP_ENGINE, sleep)
        progress.set_status("Starting the engine…")
        progress.set_progress(0)
        progress.log("Engine: " + " ".join(argv))
        engine_step = STEP_ENGINE

        def on_event(event):
            nonlocal engine_step
            kind = event.get("event")
            if kind == "progress":
                stage = event.get("stage")
                if stage in engine_runner.STAGES and STEP_ENGINE + engine_runner.STAGES.index(stage) > engine_step:
                    engine_step = STEP_ENGINE + engine_runner.STAGES.index(stage)
                    progress.set_step(engine_step)
                progress.set_status("%s…" % engine_runner.stage_label(stage))
                progress.set_progress(event.get("pct"))
            elif kind == "log":
                progress.log(event.get("line", ""))
            elif kind == "error":
                progress.log("Error: %s" % event.get("message"))

        env = config_mod.engine_environment()
        debug_log.engine_start(argv, env)
        try:
            done = engine_runner.run_engine(
                argv,
                on_event=on_event,
                should_cancel=lambda: progress.cancelled,
                should_pause=lambda: progress.paused,
                pump=progress.pump,
                env=env,
                popen=popen,
                trace=debug_log.engine_trace(),
            )
        except engine_runner.EngineCancelled as exc:
            raise Cancelled() from exc
        start_step(progress, STEP_LOAD, sleep)
        progress.set_status("Loading transcript…")
        progress.set_progress(100)

        transcript_path = done.get("transcript") or os.path.join(out_dir, "transcript.json")
        raw_transcript = transcript_mod.load_dict(transcript_path)
        transcript = transcript_mod.from_dict(raw_transcript)
        srt_paths = {str(k): str(v) for k, v in (done.get("srt") or {}).items()}
        for speaker_id in transcript.speaker_ids_with_cues():
            srt_paths.setdefault(speaker_id, os.path.join(out_dir, speaker_id + ".srt"))
        if not transcript.cues:
            raise resolve_io.ResolveError("the engine found no speech in the rendered audio (%s)" % transcript_path)

        # Speaker fixes: re-apply those saved for this audio, let the user add more, save them all.
        editor = speaker_edits.SpeakerEditor(raw_transcript)
        audio_sha = speaker_edits.audio_fingerprint(wav)
        edits_paths = [os.path.join(os.path.dirname(transcript_path), speaker_edits.EDITS_FILE)]
        store = config_mod.edits_dir(config)
        if store and audio_sha:
            edits_paths.append(os.path.join(store, audio_sha + ".json"))
        note = reapply_saved_edits(editor, edits_paths, audio_sha)

        templates = resolve_io.find_text_templates(media_pool)
        request = build_review_request(
            transcript, templates, timeline_name, clock, range_start, range_end, config, editor=editor, note=note
        )
        start_step(progress, STEP_REVIEW, sleep)  # the review dialog is its own window
    finally:
        progress.close()

    review = backend.review(request)
    debug_log.note("Review result: %r" % (review,))
    if review is None:
        raise Cancelled()
    if not review.subtitles and not review.textplus:
        raise Cancelled()

    editor.record_names(review.names)
    save_edits(editor, edits_paths, audio_sha, outcome)
    remember_review_choices(config, review, outcome)
    transcript = editor.transcript()
    if editor.relabelled:  # the engine's per-speaker SRTs no longer match
        srt_paths = transcript_mod.write_speaker_srts(transcript, os.path.join(out_dir, "edited"))

    progress = backend.progress("%s – %s" % (TITLE, timeline_name))
    try:
        progress.set_steps(STEP_LABELS)
        progress.set_step(STEP_TRACKS)
        progress.set_status("Creating tracks…")
        progress.set_progress(None)
        create_tracks(media_pool, timeline, transcript, review, templates, clock, range_start, srt_paths, outcome)
        progress.set_step(len(STEP_LABELS))
        progress.set_status("Done")
        progress.set_progress(100)
    finally:
        progress.close()
    outcome.kept_work_dir = bool(outcome.subtitle_fallbacks) or bool(config.get("keep_work_dir"))
    backend.message(TITLE, debug_log.with_path(outcome.summary()))


def reapply_saved_edits(editor, paths, audio_sha) -> str:
    """Re-apply the first saved edits for this audio; returns a note for the review dialog."""
    _path, saved = speaker_edits.find_saved_edits(paths, audio_sha)
    if not saved:
        return ""
    applied = editor.reapply(saved.get("ops"))
    notes = []
    if applied:
        notes.append(
            'Re-applied %d speaker fix%s saved from an earlier run on this audio (undo in "Fix speakers…").'
            % (applied, "" if applied == 1 else "es")
        )
    if editor.skipped:
        notes.append(
            "%d saved fix%s no longer matched the transcript and %s skipped."
            % (
                len(editor.skipped),
                "" if len(editor.skipped) == 1 else "es",
                "was" if len(editor.skipped) == 1 else "were",
            )
        )
    return " ".join(notes)


def save_edits(editor, paths, audio_sha, outcome: Outcome):
    """Write edits.json next to transcript.json and in the per-audio store; remove them when there are no edits."""
    for path in paths:
        try:
            if editor.ops:
                speaker_edits.save_edits(path, editor, audio_sha)
            elif os.path.isfile(path):
                os.remove(path)
        except OSError as exc:
            outcome.warnings.append("speaker fixes not saved to %s: %s" % (path, exc))


def build_review_request(
    transcript, templates, timeline_name, clock, range_start, range_end, config, editor=None, note: str = ""
) -> ReviewRequest:
    if editor is not None:
        rows = speaker_rows(editor)
    else:
        rows = []
        for speaker_id in transcript.speaker_ids_with_cues():
            speaker = transcript.speaker(speaker_id)
            cues = transcript.cues_for(speaker_id)
            sample = next((c.text for c in cues if c.text.strip()), "")
            rows.append(
                SpeakerRow(
                    id=speaker_id,
                    name=speaker.display_name if speaker else speaker_id,
                    cue_count=len(cues),
                    sample=sample,
                    source=speaker.name_source if speaker else None,
                )
            )
    if range_end is None:
        range_label = "whole timeline (from %s)" % clock.timecode(range_start)
    else:
        range_label = "%s – %s" % (clock.timecode(range_start), clock.timecode(range_end))
    default_mode = str(config.get("default_output", "subtitles")).lower()
    try:
        highlight_color = word_highlight.normalize_hex_color(
            config.get(config_mod.HIGHLIGHT_COLOR) or word_highlight.DEFAULT_HIGHLIGHT_COLOR
        )
    except ValueError:
        highlight_color = word_highlight.DEFAULT_HIGHLIGHT_COLOR
    return ReviewRequest(
        speakers=rows,
        templates=[t.label for t in templates],
        timeline_name=timeline_name,
        range_label=range_label,
        default_subtitles=default_mode in ("subtitles", "both"),
        default_textplus=default_mode in ("textplus", "both") and bool(templates),
        editor=editor,
        time_label=lambda seconds: clock.timecode(clock.frame_at(seconds, range_start)),
        note=note,
        highlight_words=config.get(config_mod.HIGHLIGHT_WORDS) is True,
        highlight_color=highlight_color,
        naming_provider=NAMING_CHOICES[naming_index(config.get(config_mod.NAMING_PROVIDER))][0],
    )


def remember_review_choices(config: dict, review: ReviewResult, outcome: Outcome):
    """Keep the highlight toggle and colour, and the naming provider for the next run, in config.json."""
    updates = {
        config_mod.HIGHLIGHT_WORDS: bool(review.highlight_words),
        config_mod.HIGHLIGHT_COLOR: review.highlight_color,
    }
    if review.naming_provider is not None:
        updates[config_mod.NAMING_PROVIDER] = review.naming_provider
    try:
        config_mod.save_settings(config, updates)
    except (OSError, config_mod.ConfigError) as exc:
        outcome.warnings.append("review settings not remembered: %s" % exc)


def create_tracks(
    media_pool, timeline, transcript, review: ReviewResult, templates, clock, origin_frame, srt_paths, outcome: Outcome
):
    taken_video = resolve_io.existing_track_names(timeline, "video")
    taken_subtitle = resolve_io.existing_track_names(timeline, "subtitle")
    if review.subtitles:
        plans = planning.plan_tracks(
            transcript,
            review.names,
            planning.MODE_SUBTITLES,
            clock,
            origin_frame,
            existing_track_names=taken_subtitle,
            srt_paths=srt_paths,
        )
        for plan in plans:
            try:
                outcome.created.append(resolve_io.create_subtitle_track(media_pool, timeline, plan, origin_frame))
            except resolve_io.SubtitlePlacementError as exc:
                outcome.subtitle_fallbacks.append((plan, str(exc)))
    if review.textplus:
        chosen = {}
        for speaker_id, index in review.template_index.items():
            if 0 <= index < len(templates):
                chosen[speaker_id] = templates[index]
        highlight = None
        if review.highlight_words:
            highlight = resolve_io.HighlightStyle(
                rgb=word_highlight.parse_hex_color(review.highlight_color),
                work_dir=os.path.join(outcome.work_dir or tempfile.gettempdir(), "highlight"),
            )
        plans = planning.plan_tracks(
            transcript,
            review.names,
            planning.MODE_TEXTPLUS,
            clock,
            origin_frame,
            existing_track_names=taken_video,
            templates=chosen,
            highlight=highlight is not None,
        )
        for plan in plans:
            if plan.template is None:
                outcome.warnings.append("no Text+ template chosen for %s; skipped" % plan.name)
                continue
            if highlight is not None and not any(clip.steps for clip in plan.clips):
                outcome.warnings.append("no word timings for %s's captions; placed without highlighting" % plan.name)
            try:
                outcome.created.append(
                    resolve_io.create_textplus_track(media_pool, timeline, plan, highlight=highlight)
                )
            except resolve_io.ResolveError as exc:
                outcome.warnings.append("Text+ track for %s not created: %s" % (plan.name, exc))


def _cleanup(outcome: Outcome):
    if outcome.work_dir and not outcome.kept_work_dir and os.path.isdir(outcome.work_dir):
        shutil.rmtree(outcome.work_dir, ignore_errors=True)


def _safe(fn, default):
    try:
        value = fn()
        return str(value) if value else default
    except Exception:
        return default


def _message_backend():
    """A tkinter backend just to show an error when Resolve gave no usable API; None if tkinter can't start."""
    try:
        return choose_backend(preference="tk", studio=False)
    except Exception:
        return None


def _print_or_show(backend, text):
    if backend is not None:
        try:
            backend.error(TITLE, text)
            return
        except Exception:
            pass
    print(text)
