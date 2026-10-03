"""Unit tests for free LLM routing and screening answer parsing."""

import json
import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from types import SimpleNamespace

from app.free_llm import (
    api_key_hint,
    build_free_chat_kwargs,
    clamp_max_tokens,
    get_preset,
    preset_catalog_public,
)
from app.llm_router import (
    _free_llm_user_message,
    _free_max_tokens_for_purpose,
    _free_timeout_for_purpose,
    _message_text,
    _validate_screening_output,
    free_llm_configured,
    parse_screening_answers,
)


def test_preset_catalog_has_six_models():
    models = preset_catalog_public()
    assert len(models) == 6
    ids = {m["id"] for m in models}
    assert ids == {
        "deepseek-v4-pro",
        "minimax-m3",
        "minimax-m2.7",
        "glm-5.2",
        "glm-5.3",
        "kimi-k3",
    }


def test_get_preset_known_and_unknown():
    assert get_preset("minimax-m3") is not None
    assert get_preset("glm-5.2") is not None
    glm53 = get_preset("glm-5.3")
    assert glm53 is not None
    assert glm53.model == "z-ai/glm-5.3"
    assert glm53.temperature == 0.5
    assert glm53.top_p == 1.0
    assert glm53.system_prompt == "You are a helpful assistant."
    assert glm53.test_max_tokens == 256
    assert glm53.extra_body == {
        "reasoning_effort": "high",
        "chat_template_kwargs": {"reasoning_effort": "high"},
    }
    kimi = get_preset("kimi-k3")
    assert kimi is not None
    assert kimi.model == "moonshotai/kimi-k3"
    assert kimi.extra_body == {"reasoning_effort": "high"}
    assert kimi.top_p is None
    assert kimi.content_as_parts is True
    assert kimi.test_max_tokens == 1024
    assert kimi.test_timeout_seconds == 300
    assert kimi.min_timeout_seconds == 300
    assert get_preset("unknown-model") is None


def test_glm53_payload_matches_nvidia_catalog():
    glm53 = get_preset("glm-5.3")
    assert glm53 is not None
    kwargs = build_free_chat_kwargs(glm53, "Which number is larger, 9.11 or 9.8?", 1024)
    assert kwargs["model"] == "z-ai/glm-5.3"
    assert kwargs["messages"] == [
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "Which number is larger, 9.11 or 9.8?"},
    ]
    assert kwargs["temperature"] == 0.5
    assert kwargs["top_p"] == 1.0
    assert kwargs["extra_body"] == {
        "reasoning_effort": "high",
        "chat_template_kwargs": {"reasoning_effort": "high"},
    }


def test_kimi_payload_omits_top_p_and_uses_content_parts():
    kimi = get_preset("kimi-k3")
    assert kimi is not None
    kwargs = build_free_chat_kwargs(kimi, "Hello", 256)
    assert kwargs["model"] == "moonshotai/kimi-k3"
    assert kwargs["messages"][0]["content"] == [{"type": "text", "text": "Hello"}]
    assert kwargs["temperature"] == 1.0
    assert "top_p" not in kwargs
    assert kwargs["extra_body"] == {"reasoning_effort": "high"}


def test_deepseek_payload_keeps_string_content_and_top_p():
    deepseek = get_preset("deepseek-v4-pro")
    assert deepseek is not None
    kwargs = build_free_chat_kwargs(deepseek, "Hello", 128)
    assert kwargs["messages"][0]["content"] == "Hello"
    assert kwargs["top_p"] == 0.95
    assert kwargs["extra_body"] == {"chat_template_kwargs": {"thinking": False}}


def test_message_text_ignores_reasoning_and_joins_parts():
    assert _message_text(SimpleNamespace(content="  ok  ", reasoning_content="think")) == "ok"
    assert _message_text(
        SimpleNamespace(content=[{"type": "text", "text": "hel"}, {"type": "text", "text": "lo"}])
    ) == "hello"


def test_clamp_max_tokens():
    preset = get_preset("minimax-m3")
    assert clamp_max_tokens(None, preset) == 8192
    assert clamp_max_tokens(100, preset) == 512
    assert clamp_max_tokens(99999, preset) == 16384


def test_api_key_hint_masks():
    assert api_key_hint("nvapi-abcdefghijklmnop") == "••••mnop"
    assert api_key_hint("") == ""
    assert api_key_hint("ab") == ""


def test_free_llm_configured_requires_model_and_key():
    user = SimpleNamespace(freeLlmModelId="minimax-m3", freeLlmApiKey=None)
    assert free_llm_configured(user) is False
    user.freeLlmApiKey = "nvapi-test"
    assert free_llm_configured(user) is True


def test_parse_screening_answers_json():
    raw = '{"answers": ["A one", "A two"]}'
    assert parse_screening_answers(raw, ["Q1", "Q2"]) == ["A one", "A two"]


def test_parse_screening_answers_numbered_fallback():
    raw = "1. First answer\n2. Second answer"
    assert parse_screening_answers(raw, ["Q1", "Q2"]) == ["First answer", "Second answer"]


def test_parse_screening_answers_objects_in_answers_array():
    """Free LLMs often return objects instead of plain answer strings."""
    raw = json.dumps({
        "answers": [
            {
                "question": "What have you been up to lately?",
                "answer": "Over the last two years I rebuilt CivicAI.",
                "type_used": "long_text",
                "length_chars": 447,
            },
            {
                "question": "Do you want to be the best?",
                "answer": "I focus on getting measurably better.",
                "type_used": "behavioral",
                "length_chars": 338,
            },
        ]
    })
    assert parse_screening_answers(raw, ["Q1", "Q2"]) == [
        "Over the last two years I rebuilt CivicAI.",
        "I focus on getting measurably better.",
    ]


def test_parse_screening_answers_python_literal_objects():
    """Some models emit Python dict strings (single quotes) instead of JSON."""
    raw = (
        "{'answers': ["
        "{'question': 'Q1?', 'answer': 'Plain answer one', 'type_used': 'long_text'}, "
        "{'question': 'Q2?', 'answer': 'Plain answer two', 'type_used': 'behavioral'}"
        "]}"
    )
    assert parse_screening_answers(raw, ["Q1", "Q2"]) == [
        "Plain answer one",
        "Plain answer two",
    ]


def test_parse_screening_answers_top_level_object_list():
    raw = json.dumps([
        {"question": "Q1", "answer": "First"},
        {"question": "Q2", "answer": "Second"},
    ])
    assert parse_screening_answers(raw, ["Q1", "Q2"]) == ["First", "Second"]


def test_parse_screening_answers_stringified_dict_entries():
    """Already-stringified dicts (legacy bad stores) must still unwrap."""
    raw = json.dumps({
        "answers": [
            "{'question': 'Q1?', 'answer': 'Recovered text', 'type_used': 'long_text'}",
            "Normal plain answer",
        ]
    })
    assert parse_screening_answers(raw, ["Q1", "Q2"]) == [
        "Recovered text",
        "Normal plain answer",
    ]


def test_parse_screening_answers_double_encoded_json_string():
    """Model returns a JSON string whose contents are the answers object.

    Previously this stuffed the escaped blob into Q1 and left the rest empty.
    """
    inner = {
        "answers": [
            "I led the architectural design and deployment of AI systems.",
            "I would implement comprehensive logging and monitoring.",
            "I regularly attend conferences and take online courses.",
            "I would need input from data scientists and product managers.",
        ]
    }
    raw = json.dumps(json.dumps(inner))
    qs = ["Q1", "Q2", "Q3", "Q4"]
    out = parse_screening_answers(raw, qs)
    assert out == inner["answers"]
    assert not out[0].strip().startswith("{")
    assert not out[0].strip().startswith('"')


def test_parse_screening_answers_full_payload_stuffed_in_answers_zero():
    """Full {"answers":[...]} JSON string stuffed into answers[0]."""
    inner_answers = [
        "First real answer",
        "Second real answer",
        "Third real answer",
        "Fourth real answer",
    ]
    raw = json.dumps({"answers": [json.dumps({"answers": inner_answers})]})
    out = parse_screening_answers(raw, ["Q1", "Q2", "Q3", "Q4"])
    assert out == inner_answers


def test_parse_screening_answers_stringified_answers_array_field():
    """answers field is a stringified JSON array instead of a list."""
    answers = ["Alpha", "Beta", "Gamma"]
    raw = json.dumps({"answers": json.dumps(answers)})
    assert parse_screening_answers(raw, ["Q1", "Q2", "Q3"]) == answers


def test_parse_screening_answers_multiline_email_payload():
    """Valid JSON with real newlines inside answer strings unwraps cleanly."""
    a1 = (
        "Subject: Update on Quantitative Healthcare Survey\n\n"
        "Dear [Client's Name],\n\n"
        "I wanted to update you on the status of our quantitative healthcare survey."
    )
    a2 = (
        "In a recent project, I utilized AI-driven tools like Qualtrics "
        "for survey programming and data analysis."
    )
    raw = json.dumps({"answers": [a1, a2]})
    out = parse_screening_answers(raw, ["Q1", "Q2"])
    assert out == [a1, a2]
    assert "\n\n" in out[0]
    assert not out[0].strip().startswith("{")
    assert "\\n" not in out[0]


def test_parse_screening_answers_unescaped_newlines_inside_strings():
    """Invalid JSON with raw newlines inside quoted strings still unwraps."""
    raw = (
        '{\n'
        '  "answers": [\n'
        '    "Subject: Update\n\nDear Client",\n'
        '    "Second answer here"\n'
        "  ]\n"
        "}"
    )
    out = parse_screening_answers(raw, ["Q1", "Q2"])
    assert out == ["Subject: Update\n\nDear Client", "Second answer here"]
    assert not out[0].strip().startswith("{")


def test_validate_screening_passes_after_successful_unwrap():
    _validate_screening_output(
        json.dumps({"answers": ["Plain one", "Plain two"]}),
        ["Q1", "Q2"],
    )


def test_validate_screening_rejects_unrepairable_wrapper_left_in_answer():
    """If parse cannot unwrap and leaves a wrapper-looking string, raise."""
    import pytest
    from app.llm_router import _looks_like_answers_wrapper_blob

    assert _looks_like_answers_wrapper_blob('{"answers":["a","b"]}')
    assert not _looks_like_answers_wrapper_blob(
        "I discussed answers with the team yesterday."
    )

    # Broken object with no salvageable array/list — stays as a wrapper blob.
    broken = '{"answers": oops}'
    out = parse_screening_answers(broken, ["Q1"])
    assert len(out) == 1
    assert _looks_like_answers_wrapper_blob(out[0])
    with pytest.raises(ValueError, match="unparsed answers JSON wrapper"):
        _validate_screening_output(broken, ["Q1"])


def test_free_llm_user_message_timeout():
    from openai import APITimeoutError

    msg = _free_llm_user_message(APITimeoutError("timed out"), timeout_seconds=45)
    assert "45s" in msg
    assert "timed out" in msg.lower() or "timeout" in msg.lower()


def test_free_llm_user_message_invalid_key():
    from openai import APIStatusError

    real_exc = APIStatusError.__new__(APIStatusError)
    real_exc.status_code = 401
    real_exc.message = "Unauthorized"
    msg = _free_llm_user_message(real_exc, timeout_seconds=45)
    assert "Invalid NVIDIA API key" in msg


def test_free_timeout_and_token_caps_by_purpose():
    assert _free_timeout_for_purpose("resume") == 480.0
    assert _free_timeout_for_purpose("screening") == 120.0
    kimi = get_preset("kimi-k3")
    assert _free_timeout_for_purpose("screening", kimi) == 300.0
    assert _free_timeout_for_purpose("resume", kimi) == 480.0
    assert _free_max_tokens_for_purpose("resume", 8192) == 8192
    assert _free_max_tokens_for_purpose("resume", 16384) == 16000
    assert _free_max_tokens_for_purpose("resume", 2048) == 2048
    assert _free_max_tokens_for_purpose("screening", 8192) == 8192


async def test_job_extract_asks_openai_for_temperature_zero(monkeypatch):
    """Extraction must give the same reading for the same posting run after run."""
    import app.llm_router as router

    seen = {}

    async def fake_responses(prompt, model=None, *, temperature=None):
        seen["temperature"] = temperature
        return "{}", {}

    monkeypatch.setattr(router, "get_settings", lambda: SimpleNamespace(OPENAI_API_KEY="k", RESUME_OPENAI_MODEL="gpt-4o-mini"))
    monkeypatch.setattr(router, "_openai_responses", fake_responses)
    await router.complete_with_fallback("p", None, purpose="job_extract")
    assert seen["temperature"] == 0
    await router.complete_with_fallback("p", None, purpose="resume")
    assert seen["temperature"] is None


async def test_openai_responses_sends_temperature_only_to_models_that_take_it(monkeypatch):
    import app.llm_router as router

    sent = []

    class FakeClient:
        def __init__(self, **kw):
            self.responses = self

        async def create(self, **kw):
            sent.append(kw)
            return SimpleNamespace(output_text="{}", usage=None)

    monkeypatch.setattr(router, "AsyncOpenAI", FakeClient)
    monkeypatch.setattr(router, "get_settings", lambda: SimpleNamespace(
        OPENAI_API_KEY="k", RESUME_OPENAI_MODEL="gpt-4o-mini", RESUME_REASONING_EFFORT="low"))
    await router._openai_responses("p", "gpt-4o-mini", temperature=0)
    await router._openai_responses("p", "gpt-5-mini", temperature=0)
    assert sent[0]["temperature"] == 0 and "temperature" not in sent[1]


async def test_job_extract_uses_its_own_model_when_set(monkeypatch):
    import app.llm_router as router

    seen = []

    async def fake_responses(prompt, model=None, *, temperature=None):
        seen.append(model)
        return "{}", {}

    monkeypatch.setattr(router, "_openai_responses", fake_responses)
    monkeypatch.setattr(router, "get_settings", lambda: SimpleNamespace(
        OPENAI_API_KEY="k", RESUME_OPENAI_MODEL="gpt-4o-mini", JOB_EXTRACT_MODEL="gpt-4.1-mini"))
    await router.complete_with_fallback("p", None, purpose="job_extract")
    await router.complete_with_fallback("p", None, purpose="resume")
    monkeypatch.setattr(router, "get_settings", lambda: SimpleNamespace(
        OPENAI_API_KEY="k", RESUME_OPENAI_MODEL="gpt-4o-mini", JOB_EXTRACT_MODEL=""))
    await router.complete_with_fallback("p", None, purpose="job_extract")
    assert seen == ["gpt-4.1-mini", "gpt-4o-mini", "gpt-4o-mini"]


async def test_job_extract_skips_free_model_when_extract_model_set(monkeypatch):
    """A configured JOB_EXTRACT_MODEL is the model whose accuracy was measured: use it, not a user's free preset."""
    import app.llm_router as router

    calls = []

    async def fake_free(*a, **k):
        calls.append("free")
        return '{"title":"T"}', {}

    async def fake_responses(prompt, model=None, *, temperature=None):
        calls.append(model)
        return '{"title":"T"}', {}

    preset = SimpleNamespace(model="z-ai/glm")
    monkeypatch.setattr(router, "resolve_free_preset", lambda user: (preset, 4096, "key"))
    monkeypatch.setattr(router, "_free_chat_completion", fake_free)
    monkeypatch.setattr(router, "_openai_responses", fake_responses)
    monkeypatch.setattr(router, "get_settings", lambda: SimpleNamespace(
        OPENAI_API_KEY="k", RESUME_OPENAI_MODEL="gpt-4o-mini", JOB_EXTRACT_MODEL="gpt-4.1-mini"))
    user = SimpleNamespace(id="u")
    await router.complete_with_fallback("p", user, purpose="job_extract")
    await router.complete_with_fallback("p", user, purpose="resume")
    assert calls == ["gpt-4.1-mini", "free"]
