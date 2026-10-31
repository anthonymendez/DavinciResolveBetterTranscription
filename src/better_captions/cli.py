"""Command-line entry point. The Resolve plugin invokes `better-captions run ...` as a subprocess."""

from __future__ import annotations

import argparse
import json
import os
import sys

from better_captions import __version__

# Windows without developer mode can't symlink; huggingface_hub then warns on every model load.
os.environ.setdefault("HF_HUB_DISABLE_SYMLINKS_WARNING", "1")


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
    r.add_argument("--naming", default="auto", help="none|heuristic|anthropic|openai|claude-cli|antigravity-cli|auto")
    r.add_argument("--max-chars", type=int, default=42)
    r.add_argument("--max-lines", type=int, default=2)
    r.add_argument("--max-duration", type=float, default=6.0)
    r.add_argument("--no-cache", action="store_true", help="re-transcribe even if this audio was processed before")
    r.add_argument(
        "--verbose", action="store_true", help="debug logging and tracebacks on stderr (the plugin's debug mode)"
    )

    c = sub.add_parser("cache", help="manage the transcription cache")
    c.add_argument("action", choices=["clear"])

    sub.add_parser("doctor", help="print GPU/backend detection as JSON")

    k = sub.add_parser("key", help="manage LLM API keys in the OS keychain")
    ks = k.add_subparsers(dest="key_action", required=True)
    providers = ["anthropic", "openai"]
    s = ks.add_parser("set", help="store a provider's API key (prompted, or read from stdin)")
    s.add_argument("provider", choices=providers)
    s.add_argument("--stdin", action="store_true", help="read the key from stdin instead of prompting")
    d = ks.add_parser("delete", help="remove a provider's API key from the keychain")
    d.add_argument("provider", choices=providers)
    ks.add_parser("status", help="show where each provider's key is set (never the key itself)")
    ks.add_parser("migrate", help="move plain-text keys from config.json into the keychain")
    return parser


def key_command(args) -> int:
    """`better-captions key ...`. Messages go to stdout, errors to stderr; keys are never printed."""
    import getpass

    from better_captions import secrets

    try:
        if args.key_action == "set":
            key = sys.stdin.readline() if args.stdin else getpass.getpass(f"{args.provider} API key: ")
            secrets.set_key(args.provider, key)
            print(f"{args.provider}: key stored in the OS keychain ({secrets.backend_name()})")
        elif args.key_action == "delete":
            removed = secrets.delete_key(args.provider)
            print(
                f"{args.provider}: key removed from the OS keychain"
                if removed
                else f"{args.provider}: no key in the OS keychain"
            )
        elif args.key_action == "status":
            try:
                print(f"keychain: {secrets.backend_name()}")
            except secrets.KeychainUnavailable as exc:
                print(f"keychain: {exc}")
            for provider in secrets.PROVIDERS:
                st = secrets.status(provider)
                found = [
                    name
                    for name, on in (
                        (f"env {secrets.env_var(provider)}", st["env"]),
                        ("keychain", st["keychain"]),
                        ("config.json (plain text; run `better-captions key migrate`)", st["config"]),
                    )
                    if on
                ]
                print(f"{provider}: {', '.join(found) if found else 'not set'}")
        elif args.key_action == "migrate":
            done = secrets.migrate()
            if not done:
                print(f"no API keys in {secrets.config_path()}")
            for provider, outcome in done.items():
                if outcome == "migrated":
                    print(f"{provider}: moved into the OS keychain")
                else:
                    print(f"{provider}: keychain already had a key; kept it")
            if done:
                print(f"removed the plain-text keys from {secrets.config_path()}")
    except (secrets.KeychainUnavailable, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def doctor() -> dict:
    """What the engine detects on this machine: NVIDIA GPUs, the CUDA runtime, the backend it would use,
    and the coding-agent CLIs speaker naming can use."""
    import importlib.util
    import platform

    from better_captions.engine import gpu, transcribe
    from better_captions.naming import agent_cli

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
        "naming_clis": agent_cli.found_clis(),  # {"claude": path or null, "antigravity": path or null}
    }
    try:
        report["backend"] = transcribe.choose_backend("auto")
        device, index, compute_type = transcribe.resolve_device("auto")
        report["device"] = {"device": device, "index": index, "compute_type": compute_type}
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
    return report


def enable_verbose_logging() -> None:
    """`run --verbose`: the engine's own loggers at DEBUG, everything else at INFO, all on stderr
    (stdout carries the JSON-line events)."""
    import logging

    logging.basicConfig(
        level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s: %(message)s", force=True
    )
    logging.getLogger("better_captions").setLevel(logging.DEBUG)


def run(audio: str, out: str, options) -> None:
    # The pipeline (numpy and friends) is imported on use so `doctor` still runs when those are broken.
    from better_captions.pipeline import run as pipeline_run

    pipeline_run(audio, out, options)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "doctor":
        print(json.dumps(doctor(), indent=2))
    elif args.command == "run":
        if args.verbose:
            enable_verbose_logging()
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
            cue_options=CueOptions(
                max_chars_per_line=args.max_chars, max_lines=args.max_lines, max_duration=args.max_duration
            ),
            use_cache=not args.no_cache,
        )
        try:
            run(args.audio, args.out, options)
        except Exception as exc:  # report to the plugin as a structured event
            if args.verbose:
                import traceback

                traceback.print_exc(file=sys.stderr)
            json_lines_events({"event": "error", "message": f"{type(exc).__name__}: {exc}"})
            return 1
    elif args.command == "key":
        return key_command(args)
    elif args.command == "cache":
        from better_captions.engine import cache

        print(json.dumps({"event": "cache_cleared", "removed": cache.clear()}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
