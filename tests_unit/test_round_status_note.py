"""Round status note: the recruiter's reason (canceled) or the caller's feedback (passed). mongomock, Slack stubbed."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from beanie import PydanticObjectId, init_beanie
from mongomock_motor import AsyncMongoMockClient

from app.models import Account, UserExtra
from app.models.interview import Interview, InterviewStageEvent
from app.models.interview_question import InterviewQuestion
from app.routers.interviews import flatten_rounds

pytestmark = [
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]

START = datetime(2026, 10, 7, 15, 0, tzinfo=timezone.utc)
LATER = datetime(2026, 10, 9, 15, 0, tzinfo=timezone.utc)


@pytest.fixture
async def db():
    database = AsyncMongoMockClient()["status-note-unit"]
    await init_beanie(database=database, document_models=[Interview, UserExtra, Account, InterviewQuestion])
    return database


@pytest.fixture
def client(db, monkeypatch):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.deps import get_current_user
    from app.routers import interviews

    monkeypatch.setattr(interviews, "_sync_caller_posts", AsyncMock())
    monkeypatch.setattr(interviews, "_after_save", AsyncMock())

    def make(user):
        app = FastAPI()
        app.include_router(interviews.router)
        app.dependency_overrides[get_current_user] = lambda: user
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    return make


async def make_user(name="Pedro", role="staff") -> UserExtra:
    return await UserExtra(name=name, email=f"{name.lower()}@x.io", role=role).insert()


async def make_interview(owner, *, status="scheduled", status_note="") -> Interview:
    entry = InterviewStageEvent(
        stage="intro", at=START, scheduledAt=START, endsAt=START.replace(hour=16), status=status,
        statusNote=status_note,
    )
    return await Interview(
        accountId=PydanticObjectId(), createdBy=owner.id, scheduledAt=START, companyName="Acme",
        stageHistory=[entry.model_dump()], stage="intro", status=status,
    ).insert()


def rid(iv):
    return iv.stageHistory[-1].id


async def patch(client, user, iv, body):
    async with client(user) as c:
        return await c.patch(f"/interviews/{iv.id}/stages/{rid(iv)}", json=body)


async def tip(iv):
    return (await Interview.get(iv.id)).stageHistory[-1]


async def test_cancel_with_reason_saves_the_reason(client):
    owner = await make_user()
    iv = await make_interview(owner)
    res = await patch(client, owner, iv, {"status": "canceled", "statusNote": "  Recruiter: role filled  "})
    assert res.status_code == 200
    saved = await tip(iv)
    assert saved.status == "canceled" and saved.statusNote == "Recruiter: role filled"
    assert res.json()["stageHistory"][-1]["statusNote"] == "Recruiter: role filled"


async def test_passed_with_feedback(client):
    owner = await make_user()
    iv = await make_interview(owner, status="completed")
    await patch(client, owner, iv, {"status": "passed", "statusNote": "Went well, next round Friday"})
    assert (await tip(iv)).statusNote == "Went well, next round Friday"


async def test_new_status_without_note_clears_the_old_one(client):
    owner = await make_user()
    iv = await make_interview(owner, status="canceled", status_note="Role filled")
    await patch(client, owner, iv, {"status": "scheduled"})
    saved = await tip(iv)
    assert saved.status == "scheduled" and saved.statusNote == ""


async def test_other_edits_keep_the_note(client):
    owner = await make_user()
    iv = await make_interview(owner, status="canceled", status_note="Role filled")
    await patch(client, owner, iv, {"note": "prep notes", "status": "canceled"})
    assert (await tip(iv)).statusNote == "Role filled"


async def test_note_can_be_edited_alone(client):
    owner = await make_user()
    iv = await make_interview(owner, status="canceled", status_note="Role filled")
    await patch(client, owner, iv, {"statusNote": "Role filled internally"})
    assert (await tip(iv)).statusNote == "Role filled internally"


async def test_too_long_note_is_400(client):
    owner = await make_user()
    iv = await make_interview(owner)
    res = await patch(client, owner, iv, {"status": "canceled", "statusNote": "x" * 2001})
    assert res.status_code == 400
    assert (await tip(iv)).status == "scheduled"


async def test_add_round_saves_feedback_on_the_previous_round(client):
    owner = await make_user()
    iv = await make_interview(owner, status="completed")
    body = {
        "stage": "tech_round_1", "scheduledAt": LATER.isoformat(), "status": "scheduled",
        "markPreviousPassed": True, "previousStatusNote": "Caller: strong intro",
    }
    async with client(owner) as c:
        res = await c.post(f"/interviews/{iv.id}/stages", json=body)
    assert res.status_code == 201
    history = (await Interview.get(iv.id)).stageHistory
    assert history[0].status == "passed" and history[0].statusNote == "Caller: strong intro"
    assert history[1].statusNote == ""


async def test_add_round_feedback_ignored_when_previous_not_marked(client):
    owner = await make_user()
    iv = await make_interview(owner, status="completed")
    body = {
        "stage": "tech_round_1", "scheduledAt": LATER.isoformat(),
        "markPreviousPassed": False, "previousStatusNote": "should not stick",
    }
    async with client(owner) as c:
        await c.post(f"/interviews/{iv.id}/stages", json=body)
    first = (await Interview.get(iv.id)).stageHistory[0]
    assert first.status == "completed" and first.statusNote == ""


def test_calendar_rows_carry_the_note():
    row = {
        "_id": "iv1", "companyName": "Acme",
        "stageHistory": [{"id": "r1", "stage": "intro", "status": "canceled", "scheduledAt": START,
                          "statusNote": "Role filled", "caller": {"enabled": True, "callerName": "Sam"}}],
    }
    [out] = flatten_rounds([row], from_dt=None, to_dt=None)
    assert out["statusNote"] == "Role filled"
    row["stageHistory"][0].pop("statusNote")
    [out] = flatten_rounds([row], from_dt=None, to_dt=None)
    assert out["statusNote"] is None


async def test_rejected_with_feedback(client):
    owner = await make_user()
    iv = await make_interview(owner, status="completed")
    res = await patch(client, owner, iv, {"status": "rejected", "statusNote": "Wanted more Go experience"})
    assert res.status_code == 200
    saved = await tip(iv)
    assert saved.status == "rejected" and saved.statusNote == "Wanted more Go experience"
