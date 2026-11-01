"""Score a transcript's speaker labels against a corrected reference.

    uv run python scripts/eval_speakers.py transcript.json --reference corrected.json [--json]
    uv run python scripts/eval_speakers.py transcript.json --edits edits.json [--json]

The logic lives in ``better_captions.eval_speakers``.
"""

from better_captions.eval_speakers import main

if __name__ == "__main__":
    raise SystemExit(main())
