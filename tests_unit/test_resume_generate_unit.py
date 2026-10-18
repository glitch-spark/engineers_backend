"""Manual generation (Resumes page) with a chosen resume unit, and the units in GET /accounts/lookup."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from types import SimpleNamespace

import pytest
from bson import ObjectId
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app import resume_queue
from app.deps import get_current_user
from app.models.account import ResumePrompt
from app.routers import accounts as accounts_router
from app.routers import resume as resume_router

USER = SimpleNamespace(id="u1", role="staff")


def profile(**kw):
    base = dict(
        id=ObjectId(), styleTemplate="<html>default</html>", styleTemplateName="master.html", pageFormat="A4",
        resumePromptBody="MASTER", defaultPromptId="default",
        resumePrompts=[
            ResumePrompt(id="default", name="Master", body="MASTER", templateHtml="<html>default</html>",
                         templateName="master.html"),
            ResumePrompt(id="u-fs", name="Full stack", roles=["fullstack", "devops"], body="FULLSTACK PROMPT",
                         templateHtml="<html>fs</html>", templateName="fullstack.html", pageFormat="Letter"),
            ResumePrompt(id="u-empty", name="No template yet", body="X", templateHtml="", templateName=""),
        ],
    )
    base.update(kw)
    return SimpleNamespace(**base)


@pytest.fixture
def enqueued(monkeypatch):
    calls = []
    account = profile()

    async def fake_load(account_id, user):
        return account

    async def fake_enqueue(**kw):
        calls.append(kw)
        return SimpleNamespace(id=ObjectId(), status="queued")

    monkeypatch.setattr(resume_router, "_load_owned_account", fake_load)
    monkeypatch.setattr(resume_queue, "enqueue_generation", fake_enqueue)
    return calls


async def generate(body):
    app = FastAPI()
    app.include_router(resume_router.router)
    app.dependency_overrides[get_current_user] = lambda: USER
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.post("/resume/generate", json={"accountId": "a" * 24, "company": "Acme", "jobDescription": "JD", **body})


async def test_generate_with_a_unit_uses_its_prompt_and_template(enqueued):
    r = await generate({"promptId": "u-fs"})
    assert r.status_code == 202, r.text
    kw = enqueued[-1]
    assert (kw["prompt_id"], kw["prompt_name"], kw["prompt_body"]) == ("u-fs", "Full stack", "FULLSTACK PROMPT")
    assert (kw["template_html"], kw["template_name"]) == ("<html>fs</html>", "fullstack.html")


async def test_a_one_off_prompt_still_wins_over_the_units_prompt(enqueued):
    await generate({"promptId": "u-fs", "promptBody": "ONE OFF"})
    assert enqueued[-1]["prompt_body"] == "ONE OFF" and enqueued[-1]["prompt_id"] == "u-fs"


async def test_without_a_unit_generation_is_unchanged(enqueued):
    await generate({})
    kw = enqueued[-1]
    assert kw.get("prompt_id") is None and kw.get("template_html") is None and kw["prompt_body"] is None


@pytest.mark.parametrize("prompt_id, detail", [
    ("gone", "That resume unit is no longer on this profile."),
    ("u-empty", "This resume unit has no HTML template."),
])
async def test_bad_units_are_refused(enqueued, prompt_id, detail):
    r = await generate({"promptId": prompt_id})
    assert r.status_code == 400 and r.json()["detail"] == detail
    assert enqueued == []


# ---- GET /accounts/lookup carries each profile's units (no prompt texts or HTML) ----

class FakeCursor:
    def __init__(self, rows):
        self.rows = rows

    async def to_list(self, length=None):
        return list(self.rows)


@pytest.fixture
def lookup_rows(monkeypatch):
    rows = []
    pipelines = []

    class FakeAccount:
        @staticmethod
        def get_motor_collection():
            return SimpleNamespace(aggregate=lambda p: (pipelines.append(p), FakeCursor(rows))[1])

    monkeypatch.setattr(accounts_router, "Account", FakeAccount)
    return rows, pipelines


async def lookup():
    app = FastAPI()
    app.include_router(accounts_router.router)
    app.dependency_overrides[get_current_user] = lambda: USER
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return (await client.get("/accounts/lookup")).json()["accounts"]


def row(**kw):
    base = {"_id": ObjectId(), "name": "P", "createdBy": ObjectId(), "hasTemplate": True, "hasPrompt": True}
    base.update(kw)
    return base


async def test_lookup_lists_units_with_the_default(lookup_rows):
    rows, pipelines = lookup_rows
    rows.append(row(defaultPromptId="u-fs", units=[
        {"id": "default", "name": "Master", "roles": [], "tpl": True, "templateName": "master.html"},
        {"id": "u-fs", "name": "Full stack", "roles": ["fullstack", "devops"], "tpl": True, "templateName": "fs.html"},
        {"id": "u-empty", "name": "Empty", "roles": [], "tpl": False, "templateName": ""},
    ]))
    got = (await lookup())[0]
    assert got["defaultPromptId"] == "u-fs"
    assert got["units"] == [
        {"id": "default", "name": "Master", "roles": [], "hasTemplate": True, "templateName": "master.html"},
        {"id": "u-fs", "name": "Full stack", "roles": ["fullstack", "devops"], "hasTemplate": True, "templateName": "fs.html"},
        {"id": "u-empty", "name": "Empty", "roles": [], "hasTemplate": False, "templateName": None},
    ]
    # Only small fields leave the database: no prompt bodies, no template HTML.
    project = pipelines[-1][0]["$project"]
    assert "resumePromptBody" not in project and "styleTemplate" not in project
    assert "body" not in str(project["units"]) and "resumePrompts" in str(project["units"])


async def test_lookup_reads_older_profiles_as_one_default_unit(lookup_rows):
    rows, _ = lookup_rows
    rows.append(row(promptHead="# Resume Tailoring — Master Prompt (general)\nmore", styleTemplateName="old.html"))
    rows.append(row(hasTemplate=True, styleTemplateName="old.html", units=[  # units saved before they had templates
        {"id": "default", "name": "Master", "roles": [], "tpl": None, "templateName": None},
        {"id": "u2", "name": "Backend", "role": "backend", "tpl": None, "templateName": None},
    ], defaultPromptId="u2"))
    legacy, early = await lookup()
    assert legacy["units"] == [{"id": "default", "name": "Resume Tailoring — Master Prompt", "roles": [],
                                "hasTemplate": True, "templateName": "old.html"}]
    assert legacy["defaultPromptId"] == "default"
    assert [(u["id"], u["roles"], u["hasTemplate"], u["templateName"]) for u in early["units"]] == [
        ("default", [], True, "old.html"), ("u2", ["backend"], True, "old.html")]
    assert early["defaultPromptId"] == "default"  # the master stays the default until the profile is saved
