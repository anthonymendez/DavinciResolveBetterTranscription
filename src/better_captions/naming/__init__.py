"""Infer human speaker names from transcript context."""

from __future__ import annotations

from better_captions.schema import Speaker, Transcript


def name_speakers(transcript: Transcript, provider: str = "auto", **options) -> list[Speaker]:
    """Return transcript.speakers with name/name_confidence/name_source filled where inferable.

    provider: "none", "heuristic", "anthropic", "openai" (any OpenAI-compatible endpoint, e.g. Ollama),
    or "auto" (best configured LLM, falling back to heuristic). Never raises on provider failure:
    falls back to heuristic and records the error in the returned speakers' state via logging.
    """
    raise NotImplementedError
