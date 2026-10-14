"""app/ai_usage.py: llm_router.complete() writes one ledger row per billed attempt (mongomock)."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from types import SimpleNamespace

import pytest
from beanie import PydanticObjectId, init_beanie
from mongomock_motor import AsyncMongoMockClient

from app import ai_usage, llm_router, model_catalog as mc
from app.models import AiUsageEvent
from tests_unit.test_llm_router_complete import USAGE, configured, script  # noqa: F401

pytestmark = pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning")

USER_ID = PydanticObjectId()
OTHER_ID = PydanticObjectId()


@pytest.fixture
async def db():
    database = AsyncMongoMockClient()["ai-usage-record"]
    await init_beanie(database=database, document_models=[AiUsageEvent])
    return database


async def events():
    return await AiUsageEvent.find_all().sort("+createdAt").to_list()


async def test_complete_records_feature_user_model_and_cost(db, monkeypatch):
    script(monkeypatch, {"anthropic:claude-sonnet-5-5": ("answer", USAGE)})
    await llm_router.complete("resume", "p", SimpleNamespace(id=USER_ID))
    [e] = await events()
    assert (e.feature, e.userId, e.provider, e.model) == ("resume", USER_ID, "anthropic", "claude-sonnet-5-5")
    assert (e.inputTokens, e.outputTokens, e.ok, e.fallbackUsed, e.source) == (1000, 500, True, False, "live")
    assert e.costUsd == pytest.approx((1000 * 2 + 500 * 10) / 1e6)


async def test_rejected_output_is_recorded_as_billed_but_not_ok(db, monkeypatch):
    script(monkeypatch, {
        "anthropic:claude-haiku-4-5": ("bad", USAGE),
        "anthropic:claude-sonnet-5-5": ("good", USAGE),
    })

    def validate(text):
        if text == "bad":
            raise ValueError("rejected")

    await llm_router.complete(
        "resume", "p", SimpleNamespace(id=USER_ID), model_id="anthropic:claude-haiku-4-5", validate=validate
    )
    first, second = await events()
    assert (first.model, first.ok, first.fallbackUsed) == ("claude-haiku-4-5", False, False)
    assert (second.model, second.ok, second.fallbackUsed) == ("claude-sonnet-5-5", True, True)


async def test_provider_error_before_an_answer_records_nothing(db, monkeypatch):
    script(monkeypatch, {
        "anthropic:claude-haiku-4-5": RuntimeError("down"),
        "anthropic:claude-sonnet-5-5": ("good", USAGE),
    })
    await llm_router.complete("resume", "p", None, model_id="anthropic:claude-haiku-4-5")
    assert [e.model for e in await events()] == ["claude-sonnet-5-5"]


async def test_scope_supplies_user_ref_and_feature(db, monkeypatch):
    script(monkeypatch, {"anthropic:claude-sonnet-5-5": ("answer", USAGE)})
    with ai_usage.usage_scope(user_id=USER_ID, ref_id="job-1"):
        await llm_router.complete("screening", "p", None)
        with ai_usage.usage_scope(feature="eval_resume"):
            await llm_router.complete("resume", "p", None)
    await llm_router.complete("resume", "p", None)  # outside: nothing inherited
    a, b, c = await events()
    assert (a.feature, a.userId, a.refId) == ("screening", USER_ID, "job-1")
    assert (b.feature, b.userId, b.refId) == ("eval_resume", USER_ID, "job-1")
    assert (c.feature, c.userId, c.refId) == ("resume", None, None)


async def test_explicit_user_wins_over_scope(db, monkeypatch):
    script(monkeypatch, {"anthropic:claude-sonnet-5-5": ("answer", USAGE)})
    with ai_usage.usage_scope(user_id=OTHER_ID):
        await llm_router.complete("resume", "p", SimpleNamespace(id=USER_ID))
    [e] = await events()
    assert e.userId == USER_ID


async def test_unknown_model_has_no_cost(db):
    await ai_usage.record(provider="openai", model="mystery-model", usage={"input": 10}, feature="ai_review")
    [e] = await events()
    assert (e.costUsd, e.inputTokens, e.outputTokens) == (None, 10, 0)


async def test_recorder_swallows_db_errors(db, monkeypatch):
    async def boom(self, *a, **kw):
        raise RuntimeError("mongo down")

    monkeypatch.setattr(AiUsageEvent, "insert", boom)
    await ai_usage.record(provider="openai", model="gpt-4o-mini", usage=USAGE, feature="ai_review")  # no raise


def test_openai_usage_reads_responses_and_chat_shapes():
    responses = SimpleNamespace(usage=SimpleNamespace(
        input_tokens=7, output_tokens=5, output_tokens_details=SimpleNamespace(reasoning_tokens=2)))
    chat = SimpleNamespace(usage=SimpleNamespace(prompt_tokens=3, completion_tokens=4, completion_tokens_details=None))
    assert ai_usage.openai_usage(responses) == {"input": 7, "output": 5, "reasoning": 2}
    assert ai_usage.openai_usage(chat) == {"input": 3, "output": 4}
    assert ai_usage.openai_usage(SimpleNamespace(usage=None)) == {}


async def test_truncated_anthropic_output_is_recorded_from_the_exception(db, monkeypatch):
    from app.llm_anthropic import OutputTruncated

    truncated = OutputTruncated("cut off")
    truncated.usage = {"input": 100, "output": 16000}
    script(monkeypatch, {
        "anthropic:claude-haiku-4-5": truncated,
        "anthropic:claude-sonnet-5-5": ("good", USAGE),
    })
    await llm_router.complete("resume", "p", None, model_id="anthropic:claude-haiku-4-5")
    first, second = await events()
    assert (first.model, first.ok, first.outputTokens) == ("claude-haiku-4-5", False, 16000)
    assert (second.model, second.ok) == ("claude-sonnet-5-5", True)
