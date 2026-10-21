"""Command-line entry point. The Resolve plugin invokes `better-captions run ...` as a subprocess."""

from __future__ import annotations

import argparse
import json
import sys

from better_captions import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="better-captions")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="transcribe, diarize and caption an audio file")
    r.add_argument("audio")
    r.add_argument("--out", required=True, help="output directory")
    r.add_argument("--model", default="large-v3-turbo")
    r.add_argument("--language")
    r.add_argument("--device", default="auto")
    r.add_argument("--backend", default="auto", help="auto|faster-whisper|mlx|whispercpp")
    r.add_argument("--num-speakers", type=int)
    r.add_argument("--min-speakers", type=int)
    r.add_argument("--max-speakers", type=int)
    r.add_argument("--diarization-backend", default="sherpa")
    r.add_argument("--naming", default="auto", help="none|heuristic|anthropic|openai|auto")
    r.add_argument("--max-chars", type=int, default=42)
    r.add_argument("--max-lines", type=int, default=2)
    r.add_argument("--max-duration", type=float, default=6.0)
    r.add_argument("--no-cache", action="store_true", help="re-transcribe even if this audio was processed before")

    c = sub.add_parser("cache", help="manage the transcription cache")
    c.add_argument("action", choices=["clear"])

    sub.add_parser("doctor", help="print GPU/backend detection as JSON")
    return parser


def doctor() -> dict:
    """What the engine detects on this machine: NVIDIA GPUs, the CUDA runtime, and the backend it would use."""
    import importlib.util
    import platform

    from better_captions.engine import gpu, transcribe

    runtime = gpu.setup()  # as a run would, so the CUDA checks below see the `cuda` extra
    backends = {"faster-whisper": importlib.util.find_spec("faster_whisper") is not None}
    backends.update({name: transcribe._backend_available(name) for name in transcribe.AUTO_ORDER})
    report = {
        "version": __version__,
        "platform": sys.platform,
        "machine": platform.machine(),
        "python": platform.python_version(),
        "nvidia_gpus": gpu.nvidia_gpus(),
        "cuda_runtime": runtime,
        "cuda": gpu.cuda_info(),
        "backends": backends,
    }
    try:
        report["backend"] = transcribe.choose_backend("auto")
        device, index, compute_type = transcribe.resolve_device("auto")
        report["device"] = {"device": device, "index": index, "compute_type": compute_type}
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    return report


def run(audio: str, out: str, options) -> None:
    # The pipeline (numpy and friends) is imported on use so `doctor` still runs when those are broken.
    from better_captions.pipeline import run as pipeline_run

    pipeline_run(audio, out, options)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "doctor":
        print(json.dumps(doctor(), indent=2))
    elif args.command == "run":
        from better_captions.cues import CueOptions
        from better_captions.pipeline import RunOptions, json_lines_events

        options = RunOptions(
            model=args.model,
            language=args.language,
            device=args.device,
            backend=args.backend,
            num_speakers=args.num_speakers,
            min_speakers=args.min_speakers,
            max_speakers=args.max_speakers,
            diarization_backend=args.diarization_backend,
            naming_provider=args.naming,
            cue_options=CueOptions(max_chars_per_line=args.max_chars, max_lines=args.max_lines, max_duration=args.max_duration),
            use_cache=not args.no_cache,
        )
        try:
            run(args.audio, args.out, options)
        except Exception as exc:  # report to the plugin as a structured event
            json_lines_events({"event": "error", "message": f"{type(exc).__name__}: {exc}"})
            return 1
    elif args.command == "cache":
        from better_captions.engine import cache

        print(json.dumps({"event": "cache_cleared", "removed": cache.clear()}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
