"""/resume/eval request validation and projections (router on a bare app, auth overridden, no DB)."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app import model_catalog as mc
from app.deps import get_current_user
from app.models.prompt_eval import MAX_JDS_PER_RUN
from app.routers import prompt_eval as pe


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(
        mc, "get_settings",
        lambda: SimpleNamespace(
            ANTHROPIC_API_KEY="sk-ant-test", OPENAI_API_KEY=None,
            RESUME_DEFAULT_MODEL="anthropic:claude-sonnet-5-5", COVER_LETTER_DEFAULT_MODEL="anthropic:claude-opus-5-5",
            SCREENING_DEFAULT_MODEL="anthropic:claude-sonnet-5-5", JUDGE_DEFAULT_MODEL="anthropic:claude-opus-5-5",
        ),
    )


def client(user=None):
    app = FastAPI()
    app.include_router(pe.router)
    app.dependency_overrides[get_current_user] = lambda: user or SimpleNamespace(id="u1", role="staff", resumePromptBody="")
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def account(**over):
    base = dict(id="a1", name="Profile", createdBy="u1", styleTemplate="<html><body>x</body></html>", resumePromptBody="")
    base.update(over)
    return SimpleNamespace(**base)


@pytest.fixture
def no_db(monkeypatch):
    """Anything that would touch Mongo fails loudly; validation must happen before."""

    async def boom(*a, **k):
        raise AssertionError("database must not be touched")

    monkeypatch.setattr(pe, "load_owned_account", boom)
    monkeypatch.setattr(pe, "_select_jds", boom)


# ---------- POST /runs validation order ----------


@pytest.mark.parametrize("field,value", [("resumeModel", "anthropic:nope"), ("judgeModel", "anthropic:claude-haiku-4-5")])
async def test_create_run_rejects_bad_models_before_any_db_work(no_db, field, value):
    async with client() as c:
        r = await c.post("/resume/eval/runs", json={"accountId": "a1", field: value})
    assert r.status_code == 400
    assert value.split(":")[1] in r.text or "haiku" in r.text.lower()


async def test_create_run_rejects_too_many_jd_ids_before_any_db_work(no_db):
    async with client() as c:
        r = await c.post("/resume/eval/runs", json={"accountId": "a1", "jdIds": ["x"] * (MAX_JDS_PER_RUN + 1)})
    assert r.status_code == 400 and str(MAX_JDS_PER_RUN) in r.json()["detail"]


async def test_create_run_requires_a_template(monkeypatch):
    async def owned(account_id, user):
        return account(styleTemplate="")

    monkeypatch.setattr(pe, "load_owned_account", owned)
    async with client() as c:
        r = await c.post("/resume/eval/runs", json={"accountId": "a1"})
    assert r.status_code == 400 and "template" in r.json()["detail"].lower()


async def test_create_run_404_when_profile_is_not_owned(monkeypatch):
    from fastapi import HTTPException

    async def not_owned(account_id, user):
        raise HTTPException(status_code=404, detail="not found")

    monkeypatch.setattr(pe, "load_owned_account", not_owned)
    async with client() as c:
        r = await c.post("/resume/eval/runs", json={"accountId": "a1"})
    assert r.status_code == 404


async def test_create_run_with_no_jds_is_400(monkeypatch):
    async def owned(account_id, user):
        return account()

    class Empty:
        async def to_list(self):
            return []

    monkeypatch.setattr(pe, "load_owned_account", owned)
    monkeypatch.setattr(pe.EvalJobDescription, "find", staticmethod(lambda q: Empty()))
    async with client() as c:
        r = await c.post("/resume/eval/runs", json={"accountId": "a1", "roles": ["ai"]})
    assert r.status_code == 400 and "at least one" in r.json()["detail"].lower()


async def test_create_jd_rejects_unknown_role():
    async with client() as c:
        r = await c.post("/resume/eval/jds", json={"role": "devops", "title": "t", "company": "c", "text": "x"})
    assert r.status_code == 422


async def test_update_jd_with_nothing_to_change_is_400():
    async with client() as c:
        r = await c.put("/resume/eval/jds/000000000000000000000001", json={})
    assert r.status_code == 400


# ---------- pure helpers ----------


def test_resolve_prompt_order():
    user = SimpleNamespace(resumePromptBody="  GLOBAL ")
    assert pe.resolve_prompt(" CUSTOM ", account(resumePromptBody="P"), user) == ("custom", "CUSTOM")
    assert pe.resolve_prompt("", account(resumePromptBody="P"), user) == ("custom", "")
    assert pe.resolve_prompt(None, account(resumePromptBody=" P "), user) == ("profile", "P")
    assert pe.resolve_prompt(None, account(resumePromptBody=""), user) == ("global", "GLOBAL")
    assert pe.resolve_prompt(None, account(resumePromptBody=None), SimpleNamespace(resumePromptBody=None)) == ("none", "")


def test_slim_strips_resume_text_and_html_but_keeps_the_prompt():
    run = {
        "_id": "r1", "promptBody": "P",
        "rows": [{"jdTitle": "t", "resumeText": "long", "resumeHtml": "<html/>", "verdict": {"matchScore": 1}}],
    }
    out = pe.slim(run)
    assert out["promptBody"] == "P"
    assert out["rows"] == [{"jdTitle": "t", "verdict": {"matchScore": 1}}]
    assert run["rows"][0]["resumeText"] == "long", "input must not be mutated"
