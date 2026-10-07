"""POST /resume/jobs/{id}/cover-letter: regenerate with another model, keep prior versions.

No database: the Beanie documents the route touches are replaced by in-memory fakes."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app import model_catalog as mc
from app.cover_letter import CoverLetterResult
from app.deps import get_current_user
from app.models.resume_application import CoverLetterVersion
from app.routers import resume as resume_router

JOB_ID = "a" * 24
ME = SimpleNamespace(id="u1", role="staff")


class FakeDocs:
    store: dict = {}

    @classmethod
    async def get(cls, oid):
        return cls.store.get(str(oid))


class FakeJobs(FakeDocs):
    store: dict = {}


class FakeAccounts(FakeDocs):
    store: dict = {}


class FakeUsers(FakeDocs):
    store: dict = {}


def make_job(**over):
    base = dict(
        userId="u1", accountId="acc1", companyName="Acme", jobDescription="JD", plainText="resume text",
        coverLetterText="old letter", coverLetterLlmProvider="anthropic", coverLetterLlmModel="claude-opus-5-5",
        coverLetterLlmFallbackUsed=False, coverLetterLlmFallbackReason=None, coverLetterHistory=[],
        coverLetterHook=None, coverLetterModelId=None, estimatedCostUsd=0.5, saved=0,
    )
    base.update(over)
    job = SimpleNamespace(**base)

    async def save():
        job.saved += 1

    job.save = save
    return job


@pytest.fixture(autouse=True)
def world(monkeypatch):
    FakeJobs.store = {}
    FakeAccounts.store = {"acc1": SimpleNamespace(coverLetterPrompt="account prompt")}
    FakeUsers.store = {"u1": SimpleNamespace(coverLetterPromptBody="user prompt")}
    monkeypatch.setattr(resume_router, "ResumeApplication", FakeJobs)
    monkeypatch.setattr(resume_router, "Account", FakeAccounts)
    monkeypatch.setattr(resume_router, "UserExtra", FakeUsers)
    monkeypatch.setattr(resume_router, "to_object_id", lambda v: v)
    monkeypatch.setattr(
        mc,
        "get_settings",
        lambda: SimpleNamespace(
            ANTHROPIC_API_KEY="sk-ant-test", OPENAI_API_KEY=None, RESUME_OPENAI_MODEL="gpt-5-mini",
            RESUME_REASONING_EFFORT="low", RESUME_DEFAULT_MODEL="anthropic:claude-sonnet-5-5",
            COVER_LETTER_DEFAULT_MODEL="anthropic:claude-opus-5-5", SCREENING_DEFAULT_MODEL="anthropic:claude-sonnet-5-5",
        ),
    )


@pytest.fixture
def writer(monkeypatch):
    calls = []

    async def fake(**kw):
        calls.append(kw)
        return CoverLetterResult(
            text="new letter", provider="anthropic", model="claude-fable-5-1",
            model_id="anthropic:claude-fable-5-1", fallback_used=False, fallback_reason=None, cost_usd=0.25,
        )

    monkeypatch.setattr(resume_router, "write_cover_letter", fake)
    return calls


def client(user=ME):
    app = FastAPI()
    app.include_router(resume_router.router)
    app.dependency_overrides[get_current_user] = lambda: user
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def regen(body, user=ME):
    async with client(user) as c:
        return await c.post(f"/resume/jobs/{JOB_ID}/cover-letter", json=body)


async def test_regen_replaces_text_and_archives_previous(writer):
    job = make_job()
    FakeJobs.store[JOB_ID] = job
    r = await regen({"model": "anthropic:claude-fable-5-1", "hook": " mentoring "})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["coverLetterText"] == "new letter" and out["coverLetterLlmModel"] == "claude-fable-5-1"
    assert out["estimatedCostUsd"] == pytest.approx(0.75)
    assert [v["text"] for v in out["coverLetterHistory"]] == ["old letter"]
    assert out["coverLetterHistory"][0]["model"] == "claude-opus-5-5"
    assert job.coverLetterModelId == "anthropic:claude-fable-5-1" and job.coverLetterHook == "mentoring"
    assert writer[0]["model_id"] == "anthropic:claude-fable-5-1" and writer[0]["hook"] == "mentoring"
    assert writer[0]["prompt_body"] == "account prompt" and job.saved == 1


async def test_regen_falls_back_to_user_prompt_and_stored_hook(writer):
    FakeAccounts.store = {"acc1": SimpleNamespace(coverLetterPrompt="")}
    FakeJobs.store[JOB_ID] = make_job(coverLetterHook="stored hook", coverLetterText=None)
    r = await regen({})
    assert r.status_code == 200
    assert writer[0]["prompt_body"] == "user prompt" and writer[0]["hook"] == "stored hook"
    assert r.json()["coverLetterHistory"] == []  # nothing to archive


async def test_regen_history_capped_at_five_newest_first(writer):
    old = [
        CoverLetterVersion(text=f"v{i}", provider="anthropic", model="m", createdAt=datetime.now(timezone.utc))
        for i in range(5)
    ]
    FakeJobs.store[JOB_ID] = make_job(coverLetterHistory=old)
    r = await regen({})
    texts = [v["text"] for v in r.json()["coverLetterHistory"]]
    assert texts == ["old letter", "v0", "v1", "v2", "v3"]


async def test_regen_forbidden_for_other_staff_user(writer):
    FakeJobs.store[JOB_ID] = make_job(userId="someone-else")
    r = await regen({})
    assert r.status_code == 403 and writer == []


async def test_regen_admin_can_regenerate_for_owner(writer):
    FakeJobs.store[JOB_ID] = make_job(userId="someone-else")
    FakeUsers.store["someone-else"] = SimpleNamespace(coverLetterPromptBody="")
    admin = SimpleNamespace(id="admin", role="admin")
    r = await regen({}, user=admin)
    assert r.status_code == 200


async def test_regen_missing_job_404(writer):
    r = await regen({})
    assert r.status_code == 404


@pytest.mark.parametrize("field", ["plainText", "jobDescription"])
async def test_regen_without_inputs_400(writer, field):
    FakeJobs.store[JOB_ID] = make_job(**{field: "  "})
    r = await regen({})
    assert r.status_code == 400 and writer == []


async def test_regen_archived_job_400(writer):
    FakeJobs.store[JOB_ID] = make_job(textArchivedAt=datetime(2026, 10, 7, tzinfo=timezone.utc))
    r = await regen({})
    assert r.status_code == 400 and "archived" in r.json()["detail"] and writer == []


async def test_regen_unknown_model_400(writer):
    FakeJobs.store[JOB_ID] = make_job()
    r = await regen({"model": "anthropic:nope"})
    assert r.status_code == 400 and "anthropic:nope" in r.json()["detail"] and writer == []


async def test_regen_generation_failure_is_502_and_leaves_job_untouched(monkeypatch):
    async def boom(**kw):
        raise RuntimeError("provider down")

    monkeypatch.setattr(resume_router, "write_cover_letter", boom)
    job = make_job()
    FakeJobs.store[JOB_ID] = job
    r = await regen({})
    assert r.status_code == 502 and job.coverLetterText == "old letter" and job.saved == 0
