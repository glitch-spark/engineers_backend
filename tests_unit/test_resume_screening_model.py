"""Screening answers with a selectable model (no DB; LLM router stubbed)."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app import model_catalog as mc
from app.deps import get_current_user
from app.llm_router import LlmResult
from app.routers import resume as resume_router

OWNER = SimpleNamespace(id="u1", role="staff", freeLlmModelId="glm-5.2", freeLlmApiKey="nvapi-x", freeLlmMaxTokens=None)
ACCOUNT = SimpleNamespace(name="Profile", styleTemplate="<p>Jane, Python engineer</p>")


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


@pytest.fixture
def recorder(monkeypatch):
    calls = []

    async def fake(prompt, user, questions, model_id=None):
        calls.append({"user": user, "model_id": model_id, "questions": questions})
        return LlmResult(
            text='{"answers": ["Yes, 5 years."]}',
            provider="anthropic",
            model="claude-opus-5-5",
            usage={"input": 10, "output": 5},
            fallback_used=False,
            model_id="anthropic:claude-opus-5-5",
            cost_usd=0.002,
        )

    monkeypatch.setattr(resume_router, "complete_screening", fake)
    return calls


async def run(**kw):
    return await resume_router._run_screening(
        account=ACCOUNT, jd="JD", questions=["Python?"], screening_prompt="", **kw
    )


async def test_run_screening_forwards_model_and_returns_cost(recorder):
    res = await run(owner=OWNER, use_free_llm=True, model_id="anthropic:claude-opus-5-5")
    assert recorder[0]["model_id"] == "anthropic:claude-opus-5-5" and recorder[0]["user"] is OWNER
    assert res.pairs == [{"question": "Python?", "answer": "Yes, 5 years."}]
    assert res.llm_model == "claude-opus-5-5" and res.cost_usd == 0.002


async def test_default_user_passing_keeps_standalone_off_the_free_tier(recorder):
    await run(owner=OWNER, use_free_llm=False)  # standalone, no explicit model
    assert recorder[-1]["user"] is None
    await run(owner=OWNER, use_free_llm=True)  # job-linked keeps free-first
    assert recorder[-1]["user"] is OWNER
    await run(owner=OWNER, use_free_llm=False, model_id="anthropic:claude-opus-5-5")  # explicit pick
    assert recorder[-1]["user"] is OWNER


def client():
    app = FastAPI()
    app.include_router(resume_router.router)
    app.dependency_overrides[get_current_user] = lambda: OWNER
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_standalone_screening_unknown_model_400_before_db(recorder):
    async with client() as c:
        r = await c.post(
            "/resume/screening-answers",
            json={"accountId": "a" * 24, "questions": ["Q?"], "model": "anthropic:nope"},
        )
    assert r.status_code == 400 and "anthropic:nope" in r.json()["detail"]
    assert recorder == []
