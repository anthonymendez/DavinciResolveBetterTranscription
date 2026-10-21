"""LLM speaker naming: Anthropic, or any OpenAI-compatible endpoint (OpenAI, Ollama, LM Studio).

Both providers share one prompt and one strict JSON reply format. SDKs are optional extras and are
imported lazily; every failure (missing SDK, network, refusal, bad JSON) raises so the caller can
fall back to the heuristic.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass

from better_captions.schema import Segment, Speaker

log = logging.getLogger(__name__)

DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-5-5"
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"
DEFAULT_TOKEN_BUDGET = 8000
CHARS_PER_TOKEN = 4  # rough estimate, good enough for a budget
MAX_OUTPUT_TOKENS = 4096

SYSTEM_PROMPT = """\
You identify the real names of the speakers in a diarized transcript. Speaker labels such as \
SPEAKER_00 come from automatic diarization; the names must come from the conversation itself: \
self-introductions ("I'm Sam"), people addressing each other by name ("thanks, Maria"), or \
others referring to them. Do not guess from voice, topic or stereotypes. If the transcript does \
not reveal a speaker's name, use null. Never give the same name to two speakers.

Reply with only a JSON object, no prose and no code fences, mapping every speaker label to either \
null or {"name": string, "confidence": number between 0 and 1, "evidence": short quote}."""


class NamingError(RuntimeError):
    """The LLM call failed or returned something unusable."""


@dataclass
class LLMName:
    name: str
    confidence: float
    evidence: str = ""


def build_transcript(segments: list[Segment], token_budget: int = DEFAULT_TOKEN_BUDGET) -> str:
    """Speaker-labelled transcript, one line per turn, truncated to roughly token_budget tokens.

    Over budget, keeps the start (where introductions usually are) and a shorter tail.
    """
    lines: list[str] = []
    last = None
    for seg in segments:
        text = seg.text.strip()
        if not text:
            continue
        if seg.speaker == last:
            lines[-1] += " " + text
        else:
            lines.append(f"{seg.speaker}: {text}")
            last = seg.speaker
    budget = max(1, token_budget) * CHARS_PER_TOKEN
    full = "\n".join(lines)
    if len(full) <= budget:
        return full

    head_budget, tail_budget = budget * 3 // 4, budget // 4
    head: list[str] = []
    used = 0
    for line in lines:
        if used + len(line) + 1 > head_budget:
            break
        head.append(line)
        used += len(line) + 1
    tail: list[str] = []
    used = 0
    for line in reversed(lines[len(head) :]):
        if used + len(line) + 1 > tail_budget:
            break
        tail.insert(0, line)
        used += len(line) + 1
    if not head:  # a single huge turn: cut it
        return full[:budget]
    return "\n".join([*head, "[... transcript truncated ...]", *tail])


def build_prompt(speakers: list[Speaker], segments: list[Segment], token_budget: int = DEFAULT_TOKEN_BUDGET) -> str:
    ids = ", ".join(s.id for s in speakers)
    return f"Speakers: {ids}\n\nTranscript:\n{build_transcript(segments, token_budget)}"


def parse_reply(text: str, speaker_ids: list[str]) -> dict[str, LLMName | None]:
    """Parse the model's JSON reply. Raises NamingError if it isn't the expected shape."""
    match = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not match:
        raise NamingError(f"no JSON object in reply: {text[:200]!r}")
    try:
        data = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise NamingError(f"invalid JSON in reply: {exc}") from exc
    if not isinstance(data, dict):
        raise NamingError("reply is not a JSON object")

    result: dict[str, LLMName | None] = {sid: None for sid in speaker_ids}
    for sid in speaker_ids:
        entry = data.get(sid)
        if entry is None:
            continue
        if isinstance(entry, str):  # tolerate {"SPEAKER_00": "Maria"}
            entry = {"name": entry}
        if not isinstance(entry, dict):
            raise NamingError(f"bad entry for {sid}: {entry!r}")
        name = entry.get("name")
        if name is None or (isinstance(name, str) and not name.strip()):
            continue
        if not isinstance(name, str):
            raise NamingError(f"bad name for {sid}: {name!r}")
        try:
            confidence = float(entry.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        evidence = entry.get("evidence") or ""
        result[sid] = LLMName(name.strip(), min(1.0, max(0.0, confidence)), str(evidence))

    # Never give two speakers the same name: keep the most confident claim.
    by_name: dict[str, str] = {}
    for sid, entry in sorted(
        ((sid, e) for sid, e in result.items() if e), key=lambda item: -item[1].confidence
    ):
        key = entry.name.lower()
        if key in by_name:
            result[sid] = None
        else:
            by_name[key] = sid
    return result


def _anthropic_client(api_key: str | None, base_url: str | None):
    import anthropic

    kwargs = {"api_key": api_key}
    if base_url:
        kwargs["base_url"] = base_url
    return anthropic.Anthropic(**kwargs)


def _openai_client(api_key: str | None, base_url: str | None):
    import openai

    # Local servers (Ollama, LM Studio) ignore the key, but the SDK requires one.
    return openai.OpenAI(api_key=api_key or "not-needed", base_url=base_url)


def anthropic_api_key(options: dict) -> str | None:
    return options.get("anthropic_api_key") or options.get("api_key") or os.environ.get("ANTHROPIC_API_KEY")


def openai_settings(options: dict) -> tuple[str | None, str | None]:
    """(api_key, base_url) for the OpenAI-compatible provider."""
    api_key = options.get("openai_api_key") or options.get("api_key") or os.environ.get("OPENAI_API_KEY")
    base_url = options.get("openai_base_url") or options.get("base_url") or os.environ.get("OPENAI_BASE_URL")
    return api_key, base_url


def _reply_text(content) -> str:
    return "".join(getattr(block, "text", "") for block in content if getattr(block, "type", None) == "text")


def ask_anthropic(speakers: list[Speaker], segments: list[Segment], options: dict) -> dict[str, LLMName | None]:
    api_key = anthropic_api_key(options)
    if not api_key:
        raise NamingError("no Anthropic API key (set ANTHROPIC_API_KEY or options['api_key'])")
    base_url = options.get("anthropic_base_url")
    client = _anthropic_client(api_key, base_url)
    prompt = build_prompt(speakers, segments, options.get("token_budget", DEFAULT_TOKEN_BUDGET))
    # Passed as extra_body so older SDK releases in the supported range still accept them.
    extra_body: dict = {"output_config": {"effort": "low"}}
    betas: list[str] = []
    if not base_url:  # server-side refusal fallback exists on the Claude API only
        extra_body["fallbacks"] = "default"
        betas.append("server-side-fallback-2026-07-01")
    response = client.beta.messages.create(
        model=options.get("model") or DEFAULT_ANTHROPIC_MODEL,
        max_tokens=MAX_OUTPUT_TOKENS,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
        betas=betas,
        extra_body=extra_body,
    )
    if response.stop_reason == "refusal":
        raise NamingError("model refused the request")
    if response.stop_reason == "max_tokens":
        raise NamingError("reply was cut off at max_tokens")
    return parse_reply(_reply_text(response.content), [s.id for s in speakers])


def ask_openai(speakers: list[Speaker], segments: list[Segment], options: dict) -> dict[str, LLMName | None]:
    api_key, base_url = openai_settings(options)
    client = _openai_client(api_key, base_url)
    prompt = build_prompt(speakers, segments, options.get("token_budget", DEFAULT_TOKEN_BUDGET))
    response = client.chat.completions.create(
        model=options.get("model") or os.environ.get("OPENAI_MODEL") or DEFAULT_OPENAI_MODEL,
        max_tokens=MAX_OUTPUT_TOKENS,
        temperature=0,
        messages=[{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": prompt}],
    )
    if not response.choices:
        raise NamingError("empty reply")
    return parse_reply(response.choices[0].message.content or "", [s.id for s in speakers])
