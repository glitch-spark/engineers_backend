"""POST /interviews/{id}/stages/{stage_id}/confirm: the assigned caller or an interview manager confirms."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from beanie import PydanticObjectId, init_beanie
from mongomock_motor import AsyncMongoMockClient

from app import caller_access, round_confirm
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
    database = AsyncMongoMockClient()["round-confirm-unit"]
    await init_beanie(database=database, document_models=[Interview, UserExtra, Account, InterviewQuestion])
    return database


@pytest.fixture
def slack(monkeypatch):
    from app.routers import interviews

    sync = AsyncMock(return_value=False)
    monkeypatch.setattr(interviews, "sync_caller_thread", sync)
    monkeypatch.setattr(interviews, "refresh_today", AsyncMock(return_value=False))
    return sync


@pytest.fixture
def client(db, slack):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.deps import get_current_user
    from app.routers import interviews

    def make(user):
        app = FastAPI()
        app.include_router(interviews.router)
        app.dependency_overrides[get_current_user] = lambda: user
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    return make


async def make_user(name="Pedro", role="staff") -> UserExtra:
    return await UserExtra(name=name, email=f"{name.lower()}@x.io", role=role).insert()


async def make_interview(owner, *, caller_id=None, kind="external", name="Sam", enabled=True, status="scheduled"):
    caller = InterviewCaller(
        enabled=enabled, callerId=caller_id, callerName=name, callerKind=kind, startsAt=START, method="video",
    )
    entry = InterviewStageEvent(
        stage="intro", at=START, scheduledAt=START, endsAt=START.replace(hour=16), status=status, caller=caller,
    )
    return await Interview(
        accountId=PydanticObjectId(), createdBy=owner.id, scheduledAt=START, companyName="Acme",
        stageHistory=[entry.model_dump()], stage="intro", status=status,
    ).insert()


def rid(iv):
    return iv.stageHistory[-1].id


async def confirm(client, user, iv, confirmed=True):
    async with client(user) as c:
        return await c.post(f"/interviews/{iv.id}/stages/{rid(iv)}/confirm", json={"confirmed": confirmed})


async def test_assigned_user_caller_can_confirm(client):
    owner, caller = await make_user(), await make_user("Cal")
    iv = await make_interview(owner, caller_id=str(caller.id), kind="user", name="Cal")
    r = await confirm(client, caller, iv)
    assert r.status_code == 200, r.text
    assert r.json()["stageHistory"][-1]["confirmed"] is True
    assert (await Interview.get(iv.id)).stageHistory[-1].confirmed is True


async def test_interview_manager_can_confirm_external(client):
    owner, manager = await make_user(), await make_user("Mia", "interview_manager")
    iv = await make_interview(owner, caller_id="ext1")
    r = await confirm(client, manager, iv)
    assert r.status_code == 200, r.text
    assert r.json()["stageHistory"][-1]["confirmed"] is True


async def test_owner_can_confirm_and_unconfirm(client):
    owner = await make_user()
    iv = await make_interview(owner, caller_id="ext1")
    assert (await confirm(client, owner, iv)).status_code == 200
    r = await confirm(client, owner, iv, False)
    assert r.status_code == 200 and r.json()["stageHistory"][-1]["confirmed"] is False


async def test_other_staff_cannot_confirm(client):
    owner, other = await make_user(), await make_user("Other")
    iv = await make_interview(owner, caller_id="ext1")
    r = await confirm(client, other, iv)
    assert (r.status_code, r.json()["detail"]) == (403, "Only the caller or an interview manager can confirm.")
    assert (await Interview.get(iv.id)).stageHistory[-1].confirmed is False


async def test_user_caller_of_another_round_cannot_confirm_this_one(client):
    owner, caller = await make_user(), await make_user("Cal")
    iv = await make_interview(owner, caller_id="ext1")
    r = await confirm(client, caller, iv)
    assert r.status_code == 403


async def test_tbd_round_cannot_be_confirmed(client):
    owner, manager = await make_user(), await make_user("Mia", "admin")
    iv = await make_interview(owner, caller_id=None, name="TBD")
    r = await confirm(client, manager, iv)
    assert (r.status_code, r.json()["detail"]) == (400, "Assign a caller first")


async def test_blank_name_without_a_caller_is_unassigned(client):
    owner, manager = await make_user(), await make_user("Mia", "admin")
    iv = await make_interview(owner, caller_id=None, name="")
    r = await confirm(client, manager, iv)
    assert (r.status_code, r.json()["detail"]) == (400, "Assign a caller first")


async def test_typed_name_without_a_caller_id_can_be_confirmed(client):
    # Rounds from before the caller list have a name and no id; the app shows them as assigned.
    owner, manager = await make_user(), await make_user("Mia", "admin")
    iv = await make_interview(owner, caller_id=None, name="Sam")
    r = await confirm(client, manager, iv)
    assert r.status_code == 200 and (await Interview.get(iv.id)).stageHistory[-1].confirmed is True


async def test_non_caller_round_is_400(client):
    owner = await make_user()
    iv = await make_interview(owner, enabled=False)
    r = await confirm(client, owner, iv)
    assert (r.status_code, r.json()["detail"]) == (400, "Only caller rounds are confirmed")


async def test_ended_round_cannot_be_confirmed(client):
    owner = await make_user()
    iv = await make_interview(owner, caller_id="ext1", status="canceled")
    r = await confirm(client, owner, iv)
    assert (r.status_code, r.json()["detail"]) == (400, "A canceled or rejected round can't be confirmed")


async def test_missing_interview_or_round_is_404(client):
    owner = await make_user()
    iv = await make_interview(owner, caller_id="ext1")
    async with client(owner) as c:
        r1 = await c.post(f"/interviews/{PydanticObjectId()}/stages/x/confirm", json={"confirmed": True})
        r2 = await c.post(f"/interviews/{iv.id}/stages/nope/confirm", json={"confirmed": True})
    assert r1.status_code == 404 and r2.status_code == 404


async def test_confirm_posts_thread_event(client, slack):
    owner, manager = await make_user(), await make_user("Mia", "admin")
    iv = await make_interview(owner, caller_id="ext1")
    rnd = rid(iv)
    r = await confirm(client, manager, iv)
    assert r.status_code == 200
    slack.assert_awaited_once()
    saved, before = slack.await_args.args[:2]
    assert saved.id == iv.id
    assert before[rnd]["confirmed"] is False  # the snapshot is from before the write
    assert saved.stageHistory[-1].confirmed is True


async def test_confirm_refreshes_the_day_post(client):
    from app.routers import interviews

    owner = await make_user()
    iv = await make_interview(owner, caller_id="ext1")
    await confirm(client, owner, iv)
    await interviews.drain_day_post_refreshes()
    interviews.refresh_today.assert_awaited_once_with(interview_id=str(iv.id), deleted=False)


def test_is_assigned_caller_and_can_confirm():
    caller_id = PydanticObjectId()
    user = UserExtra(id=caller_id, name="Cal", email="c@x.io", role="staff")
    stranger = UserExtra(id=PydanticObjectId(), name="S", email="s@x.io", role="staff")
    entry = InterviewStageEvent(
        stage="intro", at=START, scheduledAt=START,
        caller=InterviewCaller(enabled=True, callerId=str(caller_id), callerName="Cal", callerKind="user"),
    )
    iv = Interview(accountId=PydanticObjectId(), createdBy=PydanticObjectId(), companyName="A", stageHistory=[entry.model_dump()])
    assert caller_access.is_assigned_caller(iv, user) is True
    assert caller_access.is_assigned_caller(iv, stranger) is False
    assert caller_access.can_confirm(iv, iv.stageHistory[0], user) is True
    assert caller_access.can_confirm(iv, iv.stageHistory[0], stranger) is False


async def test_inactive_user_caller_cannot_confirm(client):
    owner = await make_user()
    caller = await UserExtra(name="Cal", email="cal@x.io", role="staff", isActive=False).insert()
    iv = await make_interview(owner, caller_id=str(caller.id), kind="user", name="Cal")
    r = await confirm(client, caller, iv)
    assert (r.status_code, r.json()["detail"]) == (403, "Only the caller or an interview manager can confirm.")
    assert (await Interview.get(iv.id)).stageHistory[-1].confirmed is False
