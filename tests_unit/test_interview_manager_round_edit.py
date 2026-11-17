"""Interview managers edit status, reason and time on anyone's round, and nothing else (mongomock, Slack stubbed)."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from beanie import PydanticObjectId, init_beanie
from mongomock_motor import AsyncMongoMockClient

from app.models import Account, Caller, UserExtra
from app.models.interview import Interview, InterviewCaller, InterviewStageEvent
from app.models.interview_question import InterviewQuestion

pytestmark = [
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]

START = datetime(2026, 10, 9, 15, 0, tzinfo=timezone.utc)
END = datetime(2026, 10, 9, 16, 0, tzinfo=timezone.utc)


@pytest.fixture
async def db():
    database = AsyncMongoMockClient()["manager-round-edit-unit"]
    await init_beanie(database=database, document_models=[Interview, UserExtra, Account, InterviewQuestion, Caller])
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


async def make_user(name="Pedro", role="staff", **extra) -> UserExtra:
    return await UserExtra(name=name, email=f"{name.lower()}@x.io", role=role, **extra).insert()


async def make_interview(owner, *, confirmed=False, caller_id=None, caller_kind="external") -> Interview:
    entry = InterviewStageEvent(
        stage="intro", at=START, scheduledAt=START, endsAt=END, status="scheduled", confirmed=confirmed,
        caller=InterviewCaller(
            enabled=True, callerName="Pietro", callerId=caller_id, callerKind=caller_kind, startsAt=START, method="video",
        ),
    )
    return await Interview(
        accountId=PydanticObjectId(), createdBy=owner.id, scheduledAt=START, companyName="Acme",
        stageHistory=[entry.model_dump()], stage="intro", status="scheduled",
    ).insert()


def rid(iv):
    return iv.stageHistory[-1].id


async def stored(iv) -> InterviewStageEvent:
    return (await Interview.get(iv.id)).stageHistory[-1]


def naive(dt: datetime) -> datetime:
    return dt.replace(tzinfo=None)


async def test_manager_sets_status_and_note_on_anyones_round(client):
    owner, puma = await make_user(), await make_user("Puma", role="interview_manager")
    iv = await make_interview(owner)
    async with client(puma) as c:
        res = await c.patch(f"/interviews/{iv.id}/stages/{rid(iv)}", json={"status": "canceled", "statusNote": "Recruiter canceled"})
    assert res.status_code == 200, res.text
    entry = await stored(iv)
    assert (entry.status.value, entry.statusNote) == ("canceled", "Recruiter canceled")


async def test_manager_reschedules_anyones_round(client):
    owner, puma = await make_user(), await make_user("Puma", role="interview_manager")
    iv = await make_interview(owner, confirmed=True)
    async with client(puma) as c:
        res = await c.patch(f"/interviews/{iv.id}/stages/{rid(iv)}", json={"scheduledAt": "2026-10-10T16:00:00Z"})
    assert res.status_code == 200, res.text
    entry = await stored(iv)
    assert naive(entry.scheduledAt) == datetime(2026, 10, 10, 16, 0)
    assert naive(entry.endsAt) == datetime(2026, 10, 10, 17, 0)  # the duration is kept
    assert entry.confirmed is False


async def test_manager_cannot_touch_other_round_fields(client):
    from app.routers.interviews import MANAGER_FIELDS_ONLY

    owner, puma = await make_user(), await make_user("Puma", role="interview_manager")
    iv = await make_interview(owner)
    before = (await stored(iv)).model_dump()
    async with client(puma) as c:
        for body in (
            {"note": "x"},
            {"status": "passed", "caller": {"enabled": False}},
            {"stage": "tech_round_1"},
        ):
            res = await c.patch(f"/interviews/{iv.id}/stages/{rid(iv)}", json=body)
            assert res.status_code == 403, body
            assert res.json()["detail"] == MANAGER_FIELDS_ONLY
    assert (await stored(iv)).model_dump() == before


async def test_staff_still_cannot_edit_others_rounds(client):
    owner, other = await make_user(), await make_user("Sam")
    iv = await make_interview(owner)
    async with client(other) as c:
        res = await c.patch(f"/interviews/{iv.id}/stages/{rid(iv)}", json={"status": "completed"})
    assert res.status_code == 403 and res.json()["detail"] == "forbidden"
    assert (await stored(iv)).status.value == "scheduled"


async def test_inactive_manager_refused(client):
    owner, puma = await make_user(), await make_user("Puma", role="interview_manager", isActive=False)
    iv = await make_interview(owner)
    async with client(puma) as c:
        res = await c.patch(f"/interviews/{iv.id}/stages/{rid(iv)}", json={"status": "completed"})
    assert res.status_code == 403
    assert (await stored(iv)).status.value == "scheduled"


async def test_manager_still_cannot_add_or_delete_rounds(client):
    owner, puma = await make_user(), await make_user("Puma", role="interview_manager")
    iv = await make_interview(owner)
    async with client(puma) as c:
        res = await c.post(f"/interviews/{iv.id}/stages", json={"stage": "tech_round_1", "scheduledAt": "2026-10-12T15:00:00Z"})
        assert res.status_code == 403, res.text
        res = await c.delete(f"/interviews/{iv.id}/stages/{rid(iv)}")
        assert res.status_code == 403, res.text
    assert len((await Interview.get(iv.id)).stageHistory) == 1


async def test_missing_interview_is_404(client):
    puma = await make_user("Puma", role="interview_manager")
    async with client(puma) as c:
        res = await c.patch(f"/interviews/{PydanticObjectId()}/stages/abc", json={"status": "completed"})
    assert res.status_code == 404 and res.json()["detail"] == "not found"


async def test_can_update_round(db):
    from app.caller_access import can_update_round

    owner, admin = await make_user(), await make_user("Admin", role="admin")
    puma, stranger = await make_user("Puma", role="interview_manager"), await make_user("Sam")
    caller_user = await make_user("Cal")
    gone = await make_user("Gone", role="interview_manager", isActive=False)
    iv = await make_interview(owner, caller_id=str(caller_user.id), caller_kind="user")
    assert can_update_round(iv, owner) and can_update_round(iv, admin) and can_update_round(iv, puma)
    assert not can_update_round(iv, stranger)
    assert not can_update_round(iv, caller_user)
    assert not can_update_round(iv, gone)
