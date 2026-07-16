"""Unit tests for free LLM routing and screening answer parsing."""

import json
import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from types import SimpleNamespace

from app.free_llm import api_key_hint, clamp_max_tokens, get_preset, preset_catalog_public
from app.llm_router import (
    _free_llm_user_message,
    _free_max_tokens_for_purpose,
    _free_timeout_for_purpose,
    free_llm_configured,
    parse_screening_answers,
)


def test_preset_catalog_has_four_models():
    models = preset_catalog_public()
    assert len(models) == 4
    ids = {m["id"] for m in models}
    assert ids == {"deepseek-v4-pro", "minimax-m3", "minimax-m2.7", "glm-5.2"}


def test_get_preset_known_and_unknown():
    assert get_preset("minimax-m3") is not None
    assert get_preset("glm-5.2") is not None
    assert get_preset("unknown-model") is None


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
    assert _free_max_tokens_for_purpose("resume", 8192) == 8192
    assert _free_max_tokens_for_purpose("resume", 16384) == 16000
    assert _free_max_tokens_for_purpose("resume", 2048) == 2048
    assert _free_max_tokens_for_purpose("screening", 8192) == 8192
