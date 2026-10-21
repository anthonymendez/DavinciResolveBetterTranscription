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
   (``word_highlight``); the toggle and colour are remembered in config.json
7. summary; the temp dir is kept only when the user still needs the SRT files
"""

from __future__ import annotations

import os
import shutil
import tempfile
import traceback

from better_captions_plugin import config as config_mod
from better_captions_plugin import engine_runner, planning, resolve_io, speaker_edits, word_highlight
from better_captions_plugin import transcript as transcript_mod
from better_captions_plugin.ui import Backend, ReviewRequest, ReviewResult, SpeakerRow, choose_backend, speaker_rows

TITLE = "Better Captions"


class Cancelled(Exception):
    pass


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
            lines.append("Created %s %d \"%s\" with %d clips." % (kind, result.track_index, result.plan.name, result.placed))
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


def main(resolve=None, fusion=None, bmd=None, backend: "Backend | None" = None, config: "dict | None" = None,
         work_dir: "str | None" = None, environ=None, popen=None, sleep=None) -> Outcome:
    """Entry point used by the launcher script (and by tests with fakes injected)."""
    outcome = Outcome()
    owns_backend = backend is None
    try:
        if config is None:
            config = config_mod.load_config(environ=environ)
        if backend is None:
            backend = choose_backend(resolve, fusion, bmd, preference=str(config.get("ui", "auto")))
        try:
            run(resolve, backend, config, outcome, work_dir=work_dir, popen=popen, sleep=sleep)
        except Cancelled:
            outcome.cancelled = True
        except (config_mod.ConfigError, resolve_io.ResolveError, engine_runner.EngineError, transcript_mod.TranscriptError) as exc:
            outcome.error = str(exc)
            backend.error(TITLE, str(exc))
        except Exception as exc:  # anything else: show it with a traceback rather than dying silently
            outcome.error = "%s: %s" % (type(exc).__name__, exc)
            backend.error(TITLE, "Unexpected error: %s\n\n%s" % (exc, traceback.format_exc()))
    except config_mod.ConfigError as exc:
        outcome.error = str(exc)
        _print_or_show(backend, str(exc))
    finally:
        _cleanup(outcome)
        if owns_backend and backend is not None:
            backend.close()
    return outcome


def run(resolve, backend: Backend, config: dict, outcome: Outcome, work_dir=None, popen=None, sleep=None):
    project, timeline = resolve_io.current_project_and_timeline(resolve)
    media_pool = project.GetMediaPool()
    clock = resolve_io.timeline_clock(timeline, project)
    range_start, range_end = resolve_io.render_range(timeline, clock)
    timeline_name = _safe(timeline.GetName, "timeline")

    outcome.work_dir = work_dir or tempfile.mkdtemp(prefix="better-captions-")
    audio_dir = os.path.join(outcome.work_dir, "audio")
    out_dir = os.path.join(outcome.work_dir, "out")

    progress = backend.progress("%s – %s" % (TITLE, timeline_name))
    try:
        progress.set_status("Rendering timeline audio…")
        progress.set_progress(None)
        render_kwargs = {"pump": progress.pump, "should_cancel": lambda: progress.cancelled}
        if sleep is not None:
            render_kwargs["sleep"] = sleep
        try:
            wav = resolve_io.render_audio(project, timeline, audio_dir, range_start, range_end, **render_kwargs)
        except resolve_io.ResolveError as exc:
            if progress.cancelled:
                raise Cancelled()
            raise exc
        progress.log("Rendered audio: %s" % wav)

        argv = config_mod.engine_command(config, wav, out_dir)
        progress.set_status("Starting the engine…")
        progress.set_progress(0)
        progress.log("Engine: " + " ".join(argv))

        def on_event(event):
            kind = event.get("event")
            if kind == "progress":
                progress.set_status("%s…" % engine_runner.stage_label(event.get("stage")))
                progress.set_progress(event.get("pct"))
            elif kind == "log":
                progress.log(event.get("line", ""))
            elif kind == "error":
                progress.log("Error: %s" % event.get("message"))

        try:
            done = engine_runner.run_engine(
                argv, on_event=on_event, should_cancel=lambda: progress.cancelled, pump=progress.pump,
                env=config_mod.engine_environment(), popen=popen,
            )
        except engine_runner.EngineCancelled:
            raise Cancelled()
        progress.set_status("Loading transcript…")
        progress.set_progress(100)
    finally:
        progress.close()

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
    request = build_review_request(transcript, templates, timeline_name, clock, range_start, range_end, config,
                                   editor=editor, note=note)
    review = backend.review(request)
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

    create_tracks(media_pool, timeline, transcript, review, templates, clock, range_start, srt_paths, outcome)
    outcome.kept_work_dir = bool(outcome.subtitle_fallbacks) or bool(config.get("keep_work_dir"))
    backend.message(TITLE, outcome.summary())


def reapply_saved_edits(editor, paths, audio_sha) -> str:
    """Re-apply the first saved edits for this audio; returns a note for the review dialog."""
    path, saved = speaker_edits.find_saved_edits(paths, audio_sha)
    if not saved:
        return ""
    applied = editor.reapply(saved.get("ops"))
    notes = []
    if applied:
        notes.append("Re-applied %d speaker fix%s saved from an earlier run on this audio (undo in \"Fix speakers…\")."
                     % (applied, "" if applied == 1 else "es"))
    if editor.skipped:
        notes.append("%d saved fix%s no longer matched the transcript and %s skipped."
                     % (len(editor.skipped), "" if len(editor.skipped) == 1 else "es", "was" if len(editor.skipped) == 1 else "were"))
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


def build_review_request(transcript, templates, timeline_name, clock, range_start, range_end, config,
                         editor=None, note: str = "") -> ReviewRequest:
    if editor is not None:
        rows = speaker_rows(editor)
    else:
        rows = []
        for speaker_id in transcript.speaker_ids_with_cues():
            speaker = transcript.speaker(speaker_id)
            cues = transcript.cues_for(speaker_id)
            sample = next((c.text for c in cues if c.text.strip()), "")
            rows.append(SpeakerRow(
                id=speaker_id,
                name=speaker.display_name if speaker else speaker_id,
                cue_count=len(cues),
                sample=sample,
                source=speaker.name_source if speaker else None,
            ))
    if range_end is None:
        range_label = "whole timeline (from %s)" % clock.timecode(range_start)
    else:
        range_label = "%s – %s" % (clock.timecode(range_start), clock.timecode(range_end))
    default_mode = str(config.get("default_output", "subtitles")).lower()
    try:
        highlight_color = word_highlight.normalize_hex_color(config.get(config_mod.HIGHLIGHT_COLOR) or word_highlight.DEFAULT_HIGHLIGHT_COLOR)
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
    )


def remember_review_choices(config: dict, review: ReviewResult, outcome: Outcome):
    """Keep the highlight toggle and colour in config.json for the next run."""
    updates = {config_mod.HIGHLIGHT_WORDS: bool(review.highlight_words), config_mod.HIGHLIGHT_COLOR: review.highlight_color}
    try:
        config_mod.save_settings(config, updates)
    except (OSError, config_mod.ConfigError) as exc:
        outcome.warnings.append("highlight settings not remembered: %s" % exc)


def create_tracks(media_pool, timeline, transcript, review: ReviewResult, templates, clock, origin_frame, srt_paths, outcome: Outcome):
    taken_video = resolve_io.existing_track_names(timeline, "video")
    taken_subtitle = resolve_io.existing_track_names(timeline, "subtitle")
    if review.subtitles:
        plans = planning.plan_tracks(transcript, review.names, planning.MODE_SUBTITLES, clock, origin_frame,
                                     existing_track_names=taken_subtitle, srt_paths=srt_paths)
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
            highlight = resolve_io.HighlightStyle(rgb=word_highlight.parse_hex_color(review.highlight_color),
                                                  work_dir=os.path.join(outcome.work_dir or tempfile.gettempdir(), "highlight"))
        plans = planning.plan_tracks(transcript, review.names, planning.MODE_TEXTPLUS, clock, origin_frame,
                                     existing_track_names=taken_video, templates=chosen, highlight=highlight is not None)
        for plan in plans:
            if plan.template is None:
                outcome.warnings.append("no Text+ template chosen for %s; skipped" % plan.name)
                continue
            if highlight is not None and not any(clip.steps for clip in plan.clips):
                outcome.warnings.append("no word timings for %s's captions; placed without highlighting" % plan.name)
            try:
                outcome.created.append(resolve_io.create_textplus_track(media_pool, timeline, plan, highlight=highlight))
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


def _print_or_show(backend, text):
    if backend is not None:
        try:
            backend.error(TITLE, text)
            return
        except Exception:
            pass
    print(text)
