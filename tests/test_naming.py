"""Speaker naming: heuristic rules, Anthropic / OpenAI-compatible providers (SDKs mocked), auto + fallback."""

import json
import logging
import sys
from types import SimpleNamespace

import pytest

from better_captions import naming
from better_captions.naming import heuristic, llm
from better_captions.schema import Segment, Speaker, Transcript


def make_transcript(*lines: tuple[str, str], speakers: list[Speaker] | None = None) -> Transcript:
    segments = [Segment(speaker=sid, start=float(i), end=i + 0.9, text=text) for i, (sid, text) in enumerate(lines)]
    ids = list(dict.fromkeys(sid for sid, _ in lines))
    return Transcript(language="en", duration=float(len(lines)), speakers=speakers or [Speaker(id=s) for s in ids], segments=segments)


DIALOGUE = make_transcript(("SPEAKER_00", "Hey Jordan, how are you?"), ("SPEAKER_01", "Good, thanks Maria."))


@pytest.fixture(autouse=True)
def no_env_keys(monkeypatch):
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL"):
        monkeypatch.delenv(var, raising=False)


def names(speakers):
    return {s.id: s.name for s in speakers}


# --- heuristic --------------------------------------------------------------------------------


def test_heuristic_vocatives_credit_the_other_speaker():
    result = naming.name_speakers(DIALOGUE, provider="heuristic")
    assert names(result) == {"SPEAKER_00": "Maria", "SPEAKER_01": "Jordan"}
    assert all(s.name_source == "heuristic" and 0 < s.name_confidence <= 0.95 for s in result)
    # Input is not mutated.
    assert DIALOGUE.speakers[0].name is None


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Hi everyone, I'm Priya and welcome to the show.", "Priya"),
        ("My name is Daniel Okafor, nice to meet you.", "Daniel Okafor"),
        ("Hello, this is Sam.", "Sam"),
        ("i am Lena by the way", "Lena"),
    ],
)
def test_heuristic_self_introductions(text, expected):
    t = make_transcript(("SPEAKER_00", text), ("SPEAKER_01", "Cool, let's get started."))
    result = naming.name_speakers(t, provider="heuristic")
    assert names(result) == {"SPEAKER_00": expected, "SPEAKER_01": None}


def test_heuristic_vocative_addressee_in_multiparty_uses_next_and_previous_speaker():
    t = make_transcript(
        ("SPEAKER_00", "What do you think, Ben?"),  # only a next speaker: SPEAKER_01
        ("SPEAKER_01", "I like it."),
        ("SPEAKER_02", "Me too. Good point, Ben."),  # split: SPEAKER_00 (next), SPEAKER_01 (previous)
        ("SPEAKER_00", "Agreed."),
    )
    scores = heuristic.score_candidates(t.segments)
    assert scores["SPEAKER_01"]["ben"].score > 0
    # Both other speakers said "Ben" to someone, so neither can be Ben.
    assert "ben" not in scores["SPEAKER_00"] and "ben" not in scores["SPEAKER_02"]
    result = naming.name_speakers(t, provider="heuristic")
    assert names(result) == {"SPEAKER_00": None, "SPEAKER_01": "Ben", "SPEAKER_02": None}

    # A single ambiguous vocative is split evenly between the previous and next speaker.
    t = make_transcript(("SPEAKER_00", "I like it."), ("SPEAKER_01", "Good point, Ben."), ("SPEAKER_02", "Thanks."))
    scores = heuristic.score_candidates(t.segments)
    assert scores["SPEAKER_00"]["ben"].score == scores["SPEAKER_02"]["ben"].score > 0


def test_heuristic_self_intro_beats_vocative_and_names_are_unique():
    t = make_transcript(
        ("SPEAKER_00", "Hey Jordan, good to see you."),
        ("SPEAKER_01", "Hi! My name is Jordan Lee, I'm new here."),
        ("SPEAKER_00", "I'm Maria. Welcome, Jordan."),
        ("SPEAKER_01", "Thanks Maria."),
        ("SPEAKER_02", "And I'm Maria too, funny."),
    )
    result = naming.name_speakers(t, provider="heuristic")
    got = names(result)
    assert got["SPEAKER_01"] == "Jordan Lee"
    assert got["SPEAKER_00"] == "Maria"
    assert got["SPEAKER_02"] is None  # Maria already taken by the stronger candidate


def test_heuristic_ignores_common_capitalised_words():
    t = make_transcript(
        ("SPEAKER_00", "Okay, I'm Sorry. Hey Guys, thanks Everyone."),
        ("SPEAKER_01", "Yeah, Monday works. Well, Okay."),
        ("SPEAKER_00", "I'm Good, thanks Dude."),
    )
    result = naming.name_speakers(t, provider="heuristic")
    assert names(result) == {"SPEAKER_00": None, "SPEAKER_01": None}
    assert all(s.name_source is None and s.name_confidence == 0 for s in result)


def test_heuristic_speaker_never_named_after_someone_they_address():
    t = make_transcript(("SPEAKER_00", "Thanks, Maria."), ("SPEAKER_00", "Maria, are you there?"))
    result = naming.name_speakers(t, provider="heuristic")
    assert names(result) == {"SPEAKER_00": None}


def test_heuristic_keeps_user_names():
    t = make_transcript(
        ("SPEAKER_00", "Hey Jordan, how are you?"),
        ("SPEAKER_01", "Good, thanks Maria."),
        speakers=[Speaker("SPEAKER_00"), Speaker("SPEAKER_01", name="Jordan", name_confidence=1.0, name_source="user")],
    )
    result = naming.name_speakers(t, provider="heuristic")
    assert names(result) == {"SPEAKER_00": "Maria", "SPEAKER_01": "Jordan"}
    assert result[1].name_source == "user"


def test_none_provider_leaves_names_empty():
    assert names(naming.name_speakers(DIALOGUE, provider="none")) == {"SPEAKER_00": None, "SPEAKER_01": None}


def test_unknown_provider_raises():
    with pytest.raises(ValueError):
        naming.name_speakers(DIALOGUE, provider="gpt")


# --- shared prompt / parsing ------------------------------------------------------------------


def test_build_transcript_labels_merges_and_truncates():
    t = make_transcript(("SPEAKER_00", "Hi."), ("SPEAKER_00", "I'm Ann."), ("SPEAKER_01", "Hello Ann."))
    assert llm.build_transcript(t.segments) == "SPEAKER_00: Hi. I'm Ann.\nSPEAKER_01: Hello Ann."

    long = make_transcript(*[(f"SPEAKER_0{i % 2}", f"line {i} " + "x" * 30) for i in range(200)])
    text = llm.build_transcript(long.segments, token_budget=100)
    assert len(text) <= 100 * llm.CHARS_PER_TOKEN + 40
    assert text.startswith("SPEAKER_00: line 0 ")
    assert "[... transcript truncated ...]" in text
    assert text.rstrip().endswith("x" * 30) and "line 199" in text


def test_parse_reply_handles_fences_nulls_and_duplicates():
    reply = "```json\n" + json.dumps(
        {
            "SPEAKER_00": {"name": "Maria", "confidence": 0.9, "evidence": "thanks Maria"},
            "SPEAKER_01": {"name": "maria", "confidence": 0.4, "evidence": "?"},
            "SPEAKER_02": None,
            "SPEAKER_03": {"name": None, "confidence": 0},
            "SPEAKER_99": {"name": "Ghost", "confidence": 1},
        }
    ) + "\n```"
    parsed = llm.parse_reply(reply, ["SPEAKER_00", "SPEAKER_01", "SPEAKER_02", "SPEAKER_03"])
    assert parsed["SPEAKER_00"] == llm.LLMName("Maria", 0.9, "thanks Maria")
    assert parsed["SPEAKER_01"] is None and parsed["SPEAKER_02"] is None and parsed["SPEAKER_03"] is None
    assert "SPEAKER_99" not in parsed


@pytest.mark.parametrize("reply", ["I think it's Maria.", "{not json}", "[1, 2]", '{"SPEAKER_00": 5}'])
def test_parse_reply_rejects_bad_json(reply):
    with pytest.raises(llm.NamingError):
        llm.parse_reply(reply, ["SPEAKER_00"])


# --- fake SDK clients -------------------------------------------------------------------------

LLM_REPLY = json.dumps(
    {
        "SPEAKER_00": {"name": "Maria", "confidence": 0.85, "evidence": "Good, thanks Maria."},
        "SPEAKER_01": {"name": "Jordan", "confidence": 0.8, "evidence": "Hey Jordan"},
    }
)


class FakeAnthropic:
    def __init__(self, reply=LLM_REPLY, stop_reason="end_turn", error=None):
        self.calls = []
        self.init = None
        self._reply, self._stop, self._error = reply, stop_reason, error
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self._error:
            raise self._error
        content = [SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=self._reply)]
        return SimpleNamespace(content=content, stop_reason=self._stop)


class FakeOpenAI:
    def __init__(self, reply=LLM_REPLY, error=None):
        self.calls = []
        self.init = None
        self._reply, self._error = reply, error
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if self._error:
            raise self._error
        return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=self._reply))])


@pytest.fixture
def fake_anthropic(monkeypatch):
    fake = FakeAnthropic()

    def factory(api_key, base_url):
        fake.init = {"api_key": api_key, "base_url": base_url}
        return fake

    monkeypatch.setattr(llm, "_anthropic_client", factory)
    return fake


@pytest.fixture
def fake_openai(monkeypatch):
    fake = FakeOpenAI()

    def factory(api_key, base_url):
        fake.init = {"api_key": api_key, "base_url": base_url}
        return fake

    monkeypatch.setattr(llm, "_openai_client", factory)
    return fake


# --- anthropic --------------------------------------------------------------------------------


def test_anthropic_provider_with_env_key(fake_anthropic, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-env")
    result = naming.name_speakers(DIALOGUE, provider="anthropic")
    assert names(result) == {"SPEAKER_00": "Maria", "SPEAKER_01": "Jordan"}
    assert [s.name_source for s in result] == ["llm", "llm"]
    assert result[0].name_confidence == 0.85

    assert fake_anthropic.init["api_key"] == "sk-env"
    (call,) = fake_anthropic.calls
    assert call["model"] == "claude-sonnet-5-5"
    assert "JSON" in call["system"] and "null" in call["system"]
    prompt = call["messages"][0]["content"]
    assert "SPEAKER_00: Hey Jordan, how are you?" in prompt
    assert "SPEAKER_01: Good, thanks Maria." in prompt


def test_anthropic_provider_options_override_key_and_model(fake_anthropic, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-env")
    naming.name_speakers(DIALOGUE, provider="anthropic", api_key="sk-opt", model="claude-opus-5-5")
    assert fake_anthropic.init["api_key"] == "sk-opt"
    assert fake_anthropic.calls[0]["model"] == "claude-opus-5-5"


def test_anthropic_null_names_stay_unnamed(fake_anthropic, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    fake_anthropic._reply = json.dumps({"SPEAKER_00": None, "SPEAKER_01": {"name": "Jordan", "confidence": 0.7, "evidence": "x"}})
    result = naming.name_speakers(DIALOGUE, provider="anthropic")
    assert names(result) == {"SPEAKER_00": None, "SPEAKER_01": "Jordan"}
    assert result[0].name_source is None


def test_anthropic_without_key_falls_back_to_heuristic(fake_anthropic, caplog):
    with caplog.at_level(logging.WARNING):
        result = naming.name_speakers(DIALOGUE, provider="anthropic")
    assert not fake_anthropic.calls
    assert names(result) == {"SPEAKER_00": "Maria", "SPEAKER_01": "Jordan"}
    assert {s.name_source for s in result} == {"heuristic"}
    assert "falling back to heuristic" in caplog.text


@pytest.mark.parametrize(
    "fake",
    [
        FakeAnthropic(reply="Sorry, I can't tell."),
        FakeAnthropic(stop_reason="refusal"),
        FakeAnthropic(error=RuntimeError("connection reset")),
    ],
    ids=["bad-json", "refusal", "exception"],
)
def test_anthropic_failures_fall_back_to_heuristic(fake, monkeypatch, caplog):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setattr(llm, "_anthropic_client", lambda api_key, base_url: fake)
    with caplog.at_level(logging.WARNING):
        result = naming.name_speakers(DIALOGUE, provider="anthropic")
    assert fake.calls
    assert names(result) == {"SPEAKER_00": "Maria", "SPEAKER_01": "Jordan"}
    assert {s.name_source for s in result} == {"heuristic"}
    assert "anthropic speaker naming failed" in caplog.text


def test_missing_sdk_falls_back_to_heuristic(monkeypatch, caplog):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setitem(sys.modules, "anthropic", None)  # import anthropic -> ImportError
    with caplog.at_level(logging.WARNING):
        result = naming.name_speakers(DIALOGUE, provider="auto")
    assert {s.name_source for s in result} == {"heuristic"}
    assert "ImportError" in caplog.text or "ModuleNotFoundError" in caplog.text


# --- openai-compatible ------------------------------------------------------------------------


def test_openai_provider_with_base_url_for_ollama(fake_openai):
    result = naming.name_speakers(DIALOGUE, provider="openai", base_url="http://localhost:11434/v1", model="llama3.1")
    assert names(result) == {"SPEAKER_00": "Maria", "SPEAKER_01": "Jordan"}
    assert [s.name_source for s in result] == ["llm", "llm"]
    assert fake_openai.init == {"api_key": None, "base_url": "http://localhost:11434/v1"}
    (call,) = fake_openai.calls
    assert call["model"] == "llama3.1"
    system, user = call["messages"]
    assert system["role"] == "system" and system["content"] == llm.SYSTEM_PROMPT
    assert "SPEAKER_00: Hey Jordan, how are you?" in user["content"]


def test_openai_provider_env_settings(fake_openai, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-oa")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-x")
    naming.name_speakers(DIALOGUE, provider="openai")
    assert fake_openai.init == {"api_key": "sk-oa", "base_url": None}
    assert fake_openai.calls[0]["model"] == "gpt-x"


def test_openai_client_factory_passes_base_url(monkeypatch):
    seen = {}
    monkeypatch.setitem(sys.modules, "openai", SimpleNamespace(OpenAI=lambda **kw: seen.update(kw) or "client"))
    assert llm._openai_client(None, "http://localhost:1234/v1") == "client"
    assert seen == {"api_key": "not-needed", "base_url": "http://localhost:1234/v1"}


def test_openai_bad_json_falls_back_to_heuristic(fake_openai, caplog):
    fake_openai._reply = "SPEAKER_00 is probably Maria"
    with caplog.at_level(logging.WARNING):
        result = naming.name_speakers(DIALOGUE, provider="openai", base_url="http://localhost:11434/v1")
    assert {s.name_source for s in result} == {"heuristic"}
    assert names(result) == {"SPEAKER_00": "Maria", "SPEAKER_01": "Jordan"}
    assert "openai speaker naming failed" in caplog.text


# --- auto -------------------------------------------------------------------------------------


def test_auto_prefers_anthropic_when_key_present(fake_anthropic, fake_openai, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    monkeypatch.setenv("OPENAI_API_KEY", "o")
    result = naming.name_speakers(DIALOGUE)
    assert fake_anthropic.calls and not fake_openai.calls
    assert {s.name_source for s in result} == {"llm"}


def test_auto_uses_openai_when_configured(fake_anthropic, fake_openai):
    result = naming.name_speakers(DIALOGUE, provider="auto", openai_base_url="http://localhost:11434/v1")
    assert fake_openai.calls and not fake_anthropic.calls
    assert {s.name_source for s in result} == {"llm"}


def test_auto_without_configuration_uses_heuristic(fake_anthropic, fake_openai):
    result = naming.name_speakers(DIALOGUE, provider="auto")
    assert not fake_anthropic.calls and not fake_openai.calls
    assert names(result) == {"SPEAKER_00": "Maria", "SPEAKER_01": "Jordan"}
    assert {s.name_source for s in result} == {"heuristic"}


def test_auto_falls_back_to_heuristic_on_provider_error(monkeypatch, caplog):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    fake = FakeAnthropic(error=TimeoutError("timed out"))
    monkeypatch.setattr(llm, "_anthropic_client", lambda api_key, base_url: fake)
    with caplog.at_level(logging.WARNING):
        result = naming.name_speakers(DIALOGUE, provider="auto")
    assert fake.calls
    assert names(result) == {"SPEAKER_00": "Maria", "SPEAKER_01": "Jordan"}
    assert {s.name_source for s in result} == {"heuristic"}
    assert "TimeoutError" in caplog.text


def test_heuristic_module_is_usable_directly():
    result = heuristic.name_speakers(DIALOGUE.speakers, DIALOGUE.segments)
    assert names(result) == {"SPEAKER_00": "Maria", "SPEAKER_01": "Jordan"}
