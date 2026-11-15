"""Confirmed flag on caller rounds (PATCH /interviews/{id}/stages/{stage_id}), mongomock, Slack stubbed."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from beanie import PydanticObjectId, init_beanie
from mongomock_motor import AsyncMongoMockClient

from app.interview_caller import round_caller_snapshot
from app.models import Account, UserExtra
from app.models.interview import Interview, InterviewCaller, InterviewStageEvent
from app.models.interview_question import InterviewQuestion

pytestmark = [
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]

START = datetime(2026, 10, 7, 15, 0, tzinfo=timezone.utc)


@pytest.fixture
async def db():
    database = AsyncMongoMockClient()["confirmed-unit"]
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


async def make_interview(owner, *, caller=True, confirmed=False, name="Sam") -> Interview:
    entry = InterviewStageEvent(
        stage="intro", at=START, scheduledAt=START, endsAt=START.replace(hour=16), status="scheduled",
        confirmed=confirmed,
        caller=InterviewCaller(enabled=caller, callerName=name, startsAt=START, method="video"),
    )
    return await Interview(
        accountId=PydanticObjectId(), createdBy=owner.id, scheduledAt=START, companyName="Acme",
        stageHistory=[entry.model_dump()], stage="intro", status="scheduled",
    ).insert()


def rid(iv):
    return iv.stageHistory[-1].id


async def patch(client, user, iv, body):
    async with client(user) as c:
        return await c.patch(f"/interviews/{iv.id}/stages/{rid(iv)}", json=body)


async def test_owner_confirms_caller_round(client):
    owner = await make_user()
    iv = await make_interview(owner)
    r = await patch(client, owner, iv, {"confirmed": True})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["stageHistory"][-1]["confirmed"] is True and body["confirmed"] is True
    saved = await Interview.get(iv.id)
    assert saved.stageHistory[-1].confirmed is True and saved.confirmed is True


@pytest.mark.parametrize("name", ["TBD", "tbd", ""])
async def test_confirm_tbd_round_is_400(client, name):
    owner = await make_user()
    iv = await make_interview(owner, name=name)
    r = await patch(client, owner, iv, {"confirmed": True})
    assert (r.status_code, r.json()["detail"]) == (400, "Assign a caller first")
    assert (await Interview.get(iv.id)).stageHistory[-1].confirmed is False


async def test_admin_can_unconfirm(client):
    owner, admin = await make_user(), await make_user("Root", "admin")
    iv = await make_interview(owner, confirmed=True)
    r = await patch(client, admin, iv, {"confirmed": False})
    assert r.status_code == 200 and r.json()["stageHistory"][-1]["confirmed"] is False


async def test_confirm_non_caller_round_is_400(client):
    owner = await make_user()
    iv = await make_interview(owner, caller=False)
    r = await patch(client, owner, iv, {"confirmed": True})
    assert (r.status_code, r.json()["detail"]) == (400, "Only caller rounds are confirmed")


async def test_reschedule_resets_confirmed(client):
    owner = await make_user()
    iv = await make_interview(owner, confirmed=True)
    r = await patch(client, owner, iv, {"scheduledAt": "2026-10-08T15:00:00Z"})
    assert r.status_code == 200, r.text
    assert r.json()["stageHistory"][-1]["confirmed"] is False and r.json()["confirmed"] is False


async def test_same_time_keeps_confirmed(client):
    owner = await make_user()
    iv = await make_interview(owner, confirmed=True)
    r = await patch(client, owner, iv, {"scheduledAt": "2026-10-07T15:00:00Z", "note": "n"})
    assert r.json()["stageHistory"][-1]["confirmed"] is True


async def test_reschedule_and_confirm_in_one_patch_stays_confirmed(client):
    owner = await make_user()
    iv = await make_interview(owner, confirmed=True)
    r = await patch(client, owner, iv, {"scheduledAt": "2026-10-08T15:00:00Z", "confirmed": True})
    assert r.json()["stageHistory"][-1]["confirmed"] is True


async def test_turning_caller_off_unconfirms(client):
    owner = await make_user()
    iv = await make_interview(owner, confirmed=True)
    r = await patch(client, owner, iv, {"caller": {"enabled": False}})
    assert r.status_code == 200, r.text
    assert r.json()["stageHistory"][-1]["confirmed"] is False


async def test_new_round_starts_unconfirmed(client):
    owner = await make_user()
    iv = await make_interview(owner, confirmed=True)
    async with client(owner) as c:
        r = await c.post(f"/interviews/{iv.id}/stages", json={
            "stage": "tech_round_1", "scheduledAt": "2026-10-09T15:00:00Z",
            "caller": {"enabled": True, "callerName": "Sam", "method": "video"},
        })
    assert r.status_code == 201, r.text
    rounds = r.json()["stageHistory"]
    assert (rounds[0]["confirmed"], rounds[-1]["confirmed"], r.json()["confirmed"]) == (True, False, False)


async def test_other_user_cannot_confirm(client):
    owner, other = await make_user(), await make_user("Maria")
    iv = await make_interview(owner)
    r = await patch(client, other, iv, {"confirmed": True})
    assert r.status_code in (403, 404)
    assert (await Interview.get(iv.id)).stageHistory[-1].confirmed is False


def test_snapshot_has_confirmed():
    entry = InterviewStageEvent(
        stage="intro", at=START, scheduledAt=START, confirmed=True,
        caller=InterviewCaller(enabled=True, startsAt=START),
    )
    assert round_caller_snapshot(entry)["confirmed"] is True


async def test_cancel_clears_confirmed(client):
    owner = await make_user()
    iv = await make_interview(owner, confirmed=True)
    r = await patch(client, owner, iv, {"status": "canceled"})
    assert r.status_code == 200, r.text
    assert r.json()["stageHistory"][-1]["confirmed"] is False and r.json()["confirmed"] is False


async def test_confirm_canceled_round_is_400(client):
    owner = await make_user()
    iv = await make_interview(owner)
    await patch(client, owner, iv, {"status": "canceled"})
    r = await patch(client, owner, iv, {"confirmed": True})
    assert (r.status_code, r.json()["detail"]) == (400, "A canceled or rejected round can't be confirmed")


async def test_legacy_put_cancel_clears_confirmed(client):
    owner = await make_user()
    iv = await make_interview(owner, confirmed=True)
    async with client(owner) as c:
        r = await c.put(f"/interviews/{iv.id}", json={"status": "canceled"})
    assert r.status_code == 200, r.text
    assert r.json()["stageHistory"][-1]["confirmed"] is False
