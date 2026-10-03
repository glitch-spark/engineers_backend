"""Unit tests for llm_router.complete(): model chain, fallback, validation, cost."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from types import SimpleNamespace

import pytest

from app import llm_router, model_catalog as mc
from app.llm_anthropic import ModelRefused, OutputTruncated


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(
        mc,
        "get_settings",
        lambda: SimpleNamespace(
            ANTHROPIC_API_KEY="sk-ant-test",
            OPENAI_API_KEY="sk-openai-test",
            RESUME_OPENAI_MODEL="gpt-5-mini",
            RESUME_REASONING_EFFORT="low",
            RESUME_DEFAULT_MODEL="anthropic:claude-sonnet-5-5",
            COVER_LETTER_DEFAULT_MODEL="anthropic:claude-opus-5-5",
            SCREENING_DEFAULT_MODEL="anthropic:claude-sonnet-5-5",
        ),
    )


def script(monkeypatch, outcomes):
    """Patch _call_spec; `outcomes` maps model id -> (text, usage) or an Exception to raise."""
    seen = []

    async def fake(spec, prompt, task, user):
        seen.append(spec.id)
        outcome = outcomes[spec.id]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(llm_router, "_call_spec", fake)
    return seen


USAGE = {"input": 1000, "output": 500}


async def test_first_model_succeeds(monkeypatch):
    seen = script(monkeypatch, {"anthropic:claude-sonnet-5-5": ("answer", USAGE)})
    res = await llm_router.complete("resume", "p", None)
    assert seen == ["anthropic:claude-sonnet-5-5"]
    assert res.text == "answer" and res.provider == "anthropic"
    assert res.model == "claude-sonnet-5-5" and res.model_id == "anthropic:claude-sonnet-5-5"
    assert res.fallback_used is False and res.fallback_reason is None
    assert res.cost_usd == pytest.approx((1000 * 2 + 500 * 10) / 1e6)


async def test_falls_back_when_first_raises(monkeypatch):
    seen = script(
        monkeypatch,
        {
            "anthropic:claude-haiku-4-5": RuntimeError("boom"),
            "anthropic:claude-sonnet-5-5": ("from sonnet", USAGE),
        },
    )
    res = await llm_router.complete("resume", "p", None, model_id="anthropic:claude-haiku-4-5")
    assert seen == ["anthropic:claude-haiku-4-5", "anthropic:claude-sonnet-5-5"]
    assert res.model_id == "anthropic:claude-sonnet-5-5"
    assert res.fallback_used is True and "boom" in (res.fallback_reason or "")


@pytest.mark.parametrize("error", [ModelRefused("declined"), OutputTruncated("cut off")])
async def test_refusal_and_truncation_fall_back(monkeypatch, error):
    seen = script(
        monkeypatch,
        {
            "anthropic:claude-sonnet-5-5": error,
            "openai:gpt-5-mini": ("ok", USAGE),
        },
    )
    res = await llm_router.complete("resume", "p", None)
    assert seen == ["anthropic:claude-sonnet-5-5", "openai:gpt-5-mini"]
    assert res.provider == "openai" and res.fallback_used is True
    assert res.cost_usd is None  # OpenAI prices unknown


async def test_validate_failure_falls_back(monkeypatch):
    seen = script(
        monkeypatch,
        {
            "anthropic:claude-sonnet-5-5": ("bad", USAGE),
            "openai:gpt-5-mini": ("good", USAGE),
        },
    )

    def validate(text):
        if text != "good":
            raise ValueError("not good enough")

    res = await llm_router.complete("resume", "p", None, validate=validate)
    assert res.text == "good" and seen == ["anthropic:claude-sonnet-5-5", "openai:gpt-5-mini"]
    assert "not good enough" in (res.fallback_reason or "")


async def test_empty_output_counts_as_failure(monkeypatch):
    script(
        monkeypatch,
        {"anthropic:claude-sonnet-5-5": ("   ", USAGE), "openai:gpt-5-mini": ("fine", USAGE)},
    )
    res = await llm_router.complete("resume", "p", None)
    assert res.text == "fine"


async def test_all_fail_raises_last_error(monkeypatch):
    script(
        monkeypatch,
        {
            "anthropic:claude-sonnet-5-5": RuntimeError("first"),
            "openai:gpt-5-mini": RuntimeError("last"),
        },
    )
    with pytest.raises(RuntimeError, match="last"):
        await llm_router.complete("resume", "p", None)


async def test_empty_chain_raises_runtime_error(monkeypatch):
    monkeypatch.setattr(
        mc,
        "get_settings",
        lambda: SimpleNamespace(
            ANTHROPIC_API_KEY=None,
            OPENAI_API_KEY=None,
            RESUME_OPENAI_MODEL="gpt-5-mini",
            RESUME_REASONING_EFFORT="low",
            RESUME_DEFAULT_MODEL="anthropic:claude-sonnet-5-5",
            COVER_LETTER_DEFAULT_MODEL="anthropic:claude-opus-5-5",
            SCREENING_DEFAULT_MODEL="anthropic:claude-sonnet-5-5",
        ),
    )
    with pytest.raises(RuntimeError, match="No model available"):
        await llm_router.complete("resume", "p", None)


async def test_unavailable_requested_model_raises_model_not_available(monkeypatch):
    script(monkeypatch, {})
    with pytest.raises(mc.ModelNotAvailable):
        await llm_router.complete("resume", "p", None, model_id="anthropic:nope")


async def test_free_first_for_screening_but_not_cover_letter(monkeypatch):
    user = SimpleNamespace(freeLlmModelId="glm-5.2", freeLlmApiKey="nvapi-x", freeLlmMaxTokens=None)
    seen = script(
        monkeypatch,
        {
            "free:glm-5.2": ("free answer", USAGE),
            "anthropic:claude-opus-5-5": ("letter", USAGE),
        },
    )
    res = await llm_router.complete("screening", "p", user)
    assert seen == ["free:glm-5.2"] and res.provider == "free" and res.cost_usd == 0.0
    seen.clear()
    res = await llm_router.complete("cover_letter", "p", user)
    assert seen == ["anthropic:claude-opus-5-5"]


async def test_complete_screening_passes_model_id(monkeypatch):
    seen = script(
        monkeypatch,
        {"anthropic:claude-opus-5-5": ('{"answers": ["yes"]}', USAGE)},
    )
    res = await llm_router.complete_screening(
        "p", None, ["Q1?"], model_id="anthropic:claude-opus-5-5"
    )
    assert seen == ["anthropic:claude-opus-5-5"] and res.model_id == "anthropic:claude-opus-5-5"


async def test_complete_resume_passes_model_id(monkeypatch):
    seen = script(monkeypatch, {"anthropic:claude-opus-5-5": ("<body></body>", USAGE)})
    await llm_router.complete_resume("p", None, model_id="anthropic:claude-opus-5-5")
    assert seen == ["anthropic:claude-opus-5-5"]
