"""Infer human speaker names from transcript context."""

from __future__ import annotations

import logging

from better_captions.naming import heuristic, llm
from better_captions.schema import Speaker, Transcript

log = logging.getLogger(__name__)

PROVIDERS = ("none", "heuristic", "anthropic", "openai", "auto")


def _resolve_auto(options: dict) -> str:
    if llm.anthropic_api_key(options):
        return "anthropic"
    api_key, base_url = llm.openai_settings(options)
    if api_key or base_url:
        return "openai"
    return "heuristic"


def _apply_llm(speakers: list[Speaker], names: dict[str, llm.LLMName | None]) -> list[Speaker]:
    out = []
    for s in speakers:
        if s.name and s.name_source == "user":
            out.append(Speaker(id=s.id, name=s.name, name_confidence=s.name_confidence, name_source="user"))
            continue
        found = names.get(s.id)
        if found:
            log.debug("%s -> %s (%.2f): %s", s.id, found.name, found.confidence, found.evidence)
            out.append(Speaker(id=s.id, name=found.name, name_confidence=found.confidence, name_source="llm"))
        else:
            out.append(Speaker(id=s.id))
    return out


def name_speakers(transcript: Transcript, provider: str = "auto", **options) -> list[Speaker]:
    """Return transcript.speakers with name/name_confidence/name_source filled where inferable.

    provider: "none", "heuristic", "anthropic", "openai" (any OpenAI-compatible endpoint, e.g. Ollama),
    or "auto" (best configured LLM, falling back to heuristic). Never raises on provider failure:
    falls back to heuristic and records the error in the returned speakers' state via logging.

    options: api_key, model, base_url, token_budget; provider-specific anthropic_api_key,
    anthropic_base_url, openai_api_key, openai_base_url. Keys also come from ANTHROPIC_API_KEY /
    OPENAI_API_KEY, then the OS keychain (better_captions.secrets); OPENAI_BASE_URL / OPENAI_MODEL.
    """
    if provider not in PROVIDERS:
        raise ValueError(f"unknown naming provider {provider!r}; expected one of {', '.join(PROVIDERS)}")
    speakers = transcript.speakers
    if provider == "none":
        return [Speaker(id=s.id, name=s.name, name_confidence=s.name_confidence, name_source=s.name_source) for s in speakers]
    if provider == "auto":
        provider = _resolve_auto(options)

    if provider in ("anthropic", "openai") and speakers:
        ask = llm.ask_anthropic if provider == "anthropic" else llm.ask_openai
        try:
            return _apply_llm(speakers, ask(speakers, transcript.segments, options))
        except Exception as exc:  # any SDK, network or parsing failure: degrade, don't fail the run
            log.warning("%s speaker naming failed (%s: %s); falling back to heuristic", provider, type(exc).__name__, exc)

    return heuristic.name_speakers(speakers, transcript.segments)
