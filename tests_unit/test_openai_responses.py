"""OpenAI Responses call: reasoning effort is sent only to the reasoning families."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from types import SimpleNamespace

import pytest

from app import llm_router


@pytest.fixture
def calls(monkeypatch):
    seen = []

    class FakeResponses:
        async def create(self, **kwargs):
            seen.append(kwargs)
            usage = SimpleNamespace(
                input_tokens=7, output_tokens=9, output_tokens_details=SimpleNamespace(reasoning_tokens=3)
            )
            return SimpleNamespace(output_text=" done ", usage=usage)

    class FakeClient:
        def __init__(self, **kwargs):
            self.responses = FakeResponses()

    monkeypatch.setattr(llm_router, "AsyncOpenAI", FakeClient)
    monkeypatch.setattr(
        llm_router,
        "get_settings",
        lambda: SimpleNamespace(OPENAI_API_KEY="sk-test", RESUME_OPENAI_MODEL="gpt-5-mini", RESUME_REASONING_EFFORT="medium"),
    )
    return seen


@pytest.mark.parametrize("model", ["gpt-6.1-sol", "gpt-6-luna", "gpt-6-astra", "gpt-5.4-mini", "gpt-5-mini"])
async def test_reasoning_models_get_the_requested_effort(calls, model):
    text, usage = await llm_router._openai_responses("p", model, effort="high")
    assert calls[-1]["reasoning"] == {"effort": "high"} and calls[-1]["model"] == model
    assert text == "done" and usage == {"input": 7, "output": 9, "reasoning": 3}


@pytest.mark.parametrize("model", ["gpt-4o-mini", "gpt-4.1"])
async def test_non_reasoning_models_get_no_reasoning_param(calls, model):
    await llm_router._openai_responses("p", model, effort="high")
    assert "reasoning" not in calls[-1]


async def test_without_an_explicit_effort_the_legacy_setting_is_used(calls):
    await llm_router._openai_responses("p", "gpt-5-mini")
    assert calls[-1]["reasoning"] == {"effort": "medium"}
