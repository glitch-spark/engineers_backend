"""Resume generation with a selectable model: cache lanes, request validation, retry reset (no DB)."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app import model_catalog as mc
from app import resume_service
from app.deps import get_current_user
from app.llm_router import LlmResult
from app.routers import resume as resume_router

TEMPLATE = "<html><head><style>.x{}</style></head><body><p>OLD</p></body></html>"
GOOD_OUTPUT = "<body><p>NEW</p></body>"


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(
        mc,
        "get_settings",
        lambda: SimpleNamespace(
            ANTHROPIC_API_KEY="sk-ant-test",
            OPENAI_API_KEY=None,
            RESUME_OPENAI_MODEL="gpt-5-mini",
            RESUME_REASONING_EFFORT="low",
            RESUME_DEFAULT_MODEL="anthropic:claude-sonnet-5-5",
            COVER_LETTER_DEFAULT_MODEL="anthropic:claude-opus-5-5",
            SCREENING_DEFAULT_MODEL="anthropic:claude-sonnet-5-5",
        ),
    )


class _Field:
    def __eq__(self, other):  # ResumeLLMCache.promptHash == key  ->  key
        return other


class FakeCache:
    store: dict = {}
    promptHash = _Field()

    def __init__(self, **kw):
        self.__dict__.update(kw)

    @classmethod
    async def find_one(cls, key):
        return cls.store.get(key)

    async def insert(self):
        FakeCache.store[self.promptHash] = self


@pytest.fixture
def cache(monkeypatch):
    FakeCache.store = {}
    monkeypatch.setattr(resume_service, "ResumeLLMCache", FakeCache)
    return FakeCache


def lane(model_id):
    spec = mc.get_model(model_id, None)
    return mc.cache_lane(spec, "resume", None)


async def test_cache_hit_on_preferred_lane_skips_llm(monkeypatch, cache):
    key = resume_service._resume_cache_key("PROMPT", lane("anthropic:claude-opus-5-5"))
    cache.store[key] = SimpleNamespace(output=GOOD_OUTPUT, model="claude-opus-5-5")

    async def boom(*a, **k):
        raise AssertionError("LLM must not be called on a cache hit")

    monkeypatch.setattr(resume_service, "complete", boom)
    res = await resume_service._resume_llm_output(
        "PROMPT", None, TEMPLATE, "anthropic:claude-opus-5-5"
    )
    assert res.text == GOOD_OUTPUT and res.model_id == "anthropic:claude-opus-5-5"
    assert res.cost_usd == 0.0 and res.usage == {"input": 0, "output": 0, "reasoning": 0}


async def test_cache_miss_calls_complete_and_stores_under_answering_lane(monkeypatch, cache):
    calls = []

    async def fake_complete(task, prompt, user, *, model_id=None, validate=None):
        calls.append((task, prompt, model_id, validate))
        return LlmResult(
            text=GOOD_OUTPUT,
            provider="anthropic",
            model="claude-sonnet-5-5",
            usage={"input": 5, "output": 7},
            fallback_used=True,
            model_id="anthropic:claude-sonnet-5-5",
            cost_usd=0.001,
        )

    monkeypatch.setattr(resume_service, "complete", fake_complete)
    res = await resume_service._resume_llm_output(
        "PROMPT", None, TEMPLATE, "anthropic:claude-haiku-4-5"
    )
    task, prompt, model_id, validate = calls[0]
    assert (task, prompt, model_id) == ("resume", "PROMPT", "anthropic:claude-haiku-4-5")
    validate(GOOD_OUTPUT)  # the router hands this callback every candidate output
    stored = cache.store[resume_service._resume_cache_key("PROMPT", lane("anthropic:claude-sonnet-5-5"))]
    assert stored.isFallback is True and stored.model == "claude-sonnet-5-5"
    assert res.model_id == "anthropic:claude-sonnet-5-5"


def user_client(authed_user=None):
    app = FastAPI()
    app.include_router(resume_router.router)
    app.dependency_overrides[get_current_user] = lambda: authed_user or SimpleNamespace(
        id="u1", role="staff", freeLlmModelId=None, freeLlmApiKey=None, freeLlmMaxTokens=None
    )
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


@pytest.mark.parametrize("field", ["resumeModel", "coverLetterModel", "screeningModel"])
async def test_generate_rejects_unknown_model_before_any_db_work(field):
    body = {"accountId": "a" * 24, "company": "Acme", "jobDescription": "JD", field: "anthropic:nope"}
    async with user_client() as client:
        r = await client.post("/resume/generate", json=body)
    assert r.status_code == 400 and "anthropic:nope" in r.json()["detail"]


def test_reset_failed_job_keeps_requested_models_and_clears_cost():
    job = SimpleNamespace(
        status="failed", step="x", errorMessage="e", startedAt=1, completedAt=1, executionMs=1,
        pdfData="a", s3Key="k", s3Url="u", plainText="p", coverLetterText="c", screeningPairs=[1],
        inputTokens=1, outputTokens=1, reasoningTokens=1,
        resumeLlmProvider="x", resumeLlmModel="x", resumeLlmFallbackUsed=True, resumeLlmFallbackReason="r",
        screeningLlmProvider="x", screeningLlmModel="x", screeningLlmFallbackUsed=True, screeningLlmFallbackReason="r",
        estimatedCostUsd=0.5,
        coverLetterLlmProvider="anthropic", coverLetterLlmModel="claude-opus-5-5",
        coverLetterLlmFallbackUsed=True, coverLetterLlmFallbackReason="r", coverLetterHistory=["old"],
        resumeModelId="anthropic:claude-opus-5-5", coverLetterModelId="anthropic:claude-fable-5-1",
        screeningModelId="anthropic:claude-sonnet-5-5", coverLetterHook="hook",
    )
    resume_router._reset_failed_job_fields(job)
    assert job.estimatedCostUsd is None
    assert job.coverLetterLlmProvider is None and job.coverLetterLlmModel is None
    assert job.coverLetterLlmFallbackUsed is None and job.coverLetterLlmFallbackReason is None
    assert job.coverLetterHistory == []
    assert job.resumeModelId == "anthropic:claude-opus-5-5"
    assert job.coverLetterModelId == "anthropic:claude-fable-5-1"
    assert job.screeningModelId == "anthropic:claude-sonnet-5-5"
    assert job.coverLetterHook == "hook"
