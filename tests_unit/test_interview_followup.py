"""The follow-up step: followUps and statusAt on a round, POST …/follow-up and the four needs of
GET /interviews/needs-input. mongomock, Slack stubbed.
docs/superpowers/specs/2026-11-20-interview-followup-design.md"""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from beanie import PydanticObjectId, init_beanie
from mongomock_motor import AsyncMongoMockClient

from app.models import Account, UserExtra
from app.models.interview import Interview, InterviewStageEvent
from app.models.interview_question import InterviewQuestion

pytestmark = [
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]

UTC = timezone.utc
START = datetime(2026, 10, 7, 15, 0, tzinfo=UTC)  # after the data start, long past
LATER = datetime(2026, 10, 9, 15, 0, tzinfo=UTC)
OLD = datetime(2026, 10, 5, 15, 0, tzinfo=UTC)  # before the data start (2026-10-06)
FUTURE = datetime(2099, 1, 5, 15, 0, tzinfo=UTC)
STAMP = datetime(2026, 10, 8, 9, 0, tzinfo=UTC)  # an earlier statusAt
COUNTS = {"outcome": 0, "follow_up": 0, "heard_back": 0, "reason": 0}


@pytest.fixture
async def db():
    database = AsyncMongoMockClient()["interview-followup-unit"]
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


def rnd(stage="intro", status="scheduled", start=START, **extra) -> dict:
    return InterviewStageEvent(
        stage=stage, at=start, scheduledAt=start, endsAt=start + timedelta(hours=1), status=status, **extra,
    ).model_dump()


def ended(ago: timedelta, **kw) -> dict:
    """A one-hour round that ended `ago` before now."""
    return rnd(start=datetime.now(UTC).replace(microsecond=0) - ago - timedelta(hours=1), **kw)


def followed(ago: timedelta, by=None) -> list[dict]:
    return [{"at": datetime.now(UTC).replace(microsecond=0) - ago, "by": by}]


async def make_interview(owner, *rounds, company="Acme", account=None) -> Interview:
    rounds = rounds or (rnd(),)
    return await Interview(
        accountId=account.id if account else PydanticObjectId(), createdBy=owner.id,
        scheduledAt=rounds[-1]["scheduledAt"], companyName=company,
        stageHistory=list(rounds), stage=rounds[-1]["stage"], status=rounds[-1]["status"],
    ).insert()


async def tip(iv) -> InterviewStageEvent:
    return (await Interview.get(iv.id)).stageHistory[-1]


def naive(dt: datetime) -> datetime:
    return dt.astimezone(UTC).replace(tzinfo=None) if dt.tzinfo else dt


def is_recent(dt: datetime) -> bool:
    return abs(datetime.now(UTC).replace(tzinfo=None) - naive(dt)) < timedelta(minutes=1)


async def follow_up(client, user, iv, stage_id=None):
    async with client(user) as c:
        return await c.post(f"/interviews/{iv.id}/stages/{stage_id or iv.stageHistory[-1].id}/follow-up")


async def needs(client, user, **params) -> dict:
    async with client(user) as c:
        res = await c.get("/interviews/needs-input", params=params)
    assert res.status_code == 200, res.text
    return res.json()


# --- POST …/follow-up ---------------------------------------------------------------------------------------


async def test_follow_up_is_appended_and_returned(client):
    owner = await make_user()
    iv = await make_interview(owner, rnd(status="completed"))
    res = await follow_up(client, owner, iv)
    assert res.status_code == 200, res.text
    [saved] = (await tip(iv)).followUps
    assert saved.by == owner.id and is_recent(saved.at)
    out = res.json()
    assert out["_id"] == str(iv.id) and out["companyName"] == "Acme"  # the interview
    [shown] = out["stageHistory"][-1]["followUps"]
    assert shown == {"at": shown["at"], "by": str(owner.id)} and shown["at"].endswith("Z")
    assert (await tip(iv)).status.value == "completed"


async def test_a_second_follow_up_is_a_second_entry(client):
    owner = await make_user()
    iv = await make_interview(owner, rnd(status="completed"))
    await follow_up(client, owner, iv)
    assert (await follow_up(client, owner, iv)).status_code == 200
    assert len((await tip(iv)).followUps) == 2


@pytest.mark.parametrize("status", ["scheduled", "passed", "rejected", "canceled"])
async def test_follow_up_needs_a_completed_round(client, status):
    owner = await make_user()
    iv = await make_interview(owner, rnd(status=status))
    res = await follow_up(client, owner, iv)
    assert res.status_code == 400 and res.json()["detail"] == "Only a completed round can be followed up"
    assert (await tip(iv)).followUps == []


async def test_follow_up_access_is_the_status_change_access(client):
    owner, stranger = await make_user(), await make_user("Sam")
    puma, admin = await make_user("Puma", role="interview_manager"), await make_user("Root", role="admin")
    iv = await make_interview(owner, rnd(status="completed"))
    res = await follow_up(client, stranger, iv)
    assert res.status_code == 403
    assert (await tip(iv)).followUps == []
    assert (await follow_up(client, puma, iv)).status_code == 200
    assert (await follow_up(client, admin, iv)).status_code == 200
    assert [f.by for f in (await tip(iv)).followUps] == [puma.id, admin.id]


async def test_follow_up_on_a_missing_round_is_404(client):
    owner = await make_user()
    iv = await make_interview(owner, rnd(status="completed"))
    assert (await follow_up(client, owner, iv, stage_id="nope")).status_code == 404
    async with client(owner) as c:
        assert (await c.post(f"/interviews/{PydanticObjectId()}/stages/x/follow-up")).status_code == 404


# --- statusAt ----------------------------------------------------------------------------------------------


async def test_round_json_carries_follow_ups_and_status_at(client):
    owner = await make_user()
    iv = await make_interview(owner)
    async with client(owner) as c:
        out = (await c.get(f"/interviews/{iv.id}")).json()["stageHistory"][-1]
    assert out["followUps"] == [] and out["statusAt"] is None


async def test_patch_status_change_sets_status_at(client):
    owner = await make_user()
    iv = await make_interview(owner)
    async with client(owner) as c:
        res = await c.patch(f"/interviews/{iv.id}/stages/{iv.stageHistory[-1].id}", json={"status": "completed"})
    assert res.status_code == 200, res.text
    assert is_recent((await tip(iv)).statusAt)
    assert res.json()["stageHistory"][-1]["statusAt"].endswith("Z")


@pytest.mark.parametrize("body", [
    {"note": "prep"}, {"status": "completed"}, {"status": "completed", "note": "x"},
    {"scheduledAt": LATER.isoformat()},
])
async def test_patch_that_keeps_the_status_keeps_status_at(client, body):
    owner = await make_user()
    iv = await make_interview(owner, rnd(status="completed", statusAt=STAMP))
    async with client(owner) as c:
        res = await c.patch(f"/interviews/{iv.id}/stages/{iv.stageHistory[-1].id}", json=body)
    assert res.status_code == 200, res.text
    assert naive((await tip(iv)).statusAt) == naive(STAMP)


async def test_legacy_put_status_change_sets_status_at(client):
    owner = await make_user()
    iv = await make_interview(owner, rnd(status="completed", statusAt=STAMP))
    async with client(owner) as c:
        kept = await c.put(f"/interviews/{iv.id}", json={"companyName": "Acme Inc"})
        assert kept.status_code == 200, kept.text
        assert naive((await tip(iv)).statusAt) == naive(STAMP)
        same = await c.put(f"/interviews/{iv.id}", json={"status": "completed"})
        assert same.status_code == 200, same.text
        assert naive((await tip(iv)).statusAt) == naive(STAMP)
        res = await c.put(f"/interviews/{iv.id}", json={"status": "passed"})
    assert res.status_code == 200, res.text
    saved = await tip(iv)
    assert saved.status.value == "passed" and is_recent(saved.statusAt)


async def test_legacy_put_stage_move_leaves_the_new_round_without_status_at(client):
    owner = await make_user()
    iv = await make_interview(owner, rnd(status="completed", statusAt=STAMP))
    async with client(owner) as c:
        res = await c.put(f"/interviews/{iv.id}", json={"stage": "final", "scheduledAt": FUTURE.isoformat()})
    assert res.status_code == 200, res.text
    first, new = (await Interview.get(iv.id)).stageHistory
    assert new.status.value == "scheduled" and new.statusAt is None
    assert naive(first.statusAt) == naive(STAMP)


async def test_add_round_marking_the_previous_passed_sets_its_status_at(client):
    owner = await make_user()
    iv = await make_interview(owner, rnd(status="completed", statusAt=STAMP))
    body = {"stage": "final", "scheduledAt": FUTURE.isoformat(), "markPreviousPassed": True}
    async with client(owner) as c:
        res = await c.post(f"/interviews/{iv.id}/stages", json=body)
    assert res.status_code == 201, res.text
    first, new = (await Interview.get(iv.id)).stageHistory
    assert first.status.value == "passed" and is_recent(first.statusAt)
    assert new.statusAt is None  # a new round has not changed status


async def test_add_round_without_marking_keeps_the_previous_status_at(client):
    owner = await make_user()
    iv = await make_interview(owner, rnd(status="passed", statusAt=STAMP))
    body = {"stage": "final", "scheduledAt": FUTURE.isoformat(), "markPreviousPassed": True}
    async with client(owner) as c:
        assert (await c.post(f"/interviews/{iv.id}/stages", json=body)).status_code == 201
    assert naive((await Interview.get(iv.id)).stageHistory[0].statusAt) == naive(STAMP)


async def test_reject_endpoint_sets_status_at(client):
    owner, puma = await make_user(), await make_user("Puma", role="interview_manager")
    iv = await make_interview(owner, rnd(status="completed", statusAt=STAMP))
    async with client(puma) as c:
        res = await c.post(f"/interviews/{iv.id}/stages/{iv.stageHistory[-1].id}/reject")
    assert res.status_code == 200, res.text
    saved = await tip(iv)
    assert saved.status.value == "rejected" and is_recent(saved.statusAt)


async def test_follow_up_does_not_touch_status_at(client):
    owner = await make_user()
    iv = await make_interview(owner, rnd(status="completed", statusAt=STAMP))
    await follow_up(client, owner, iv)
    assert naive((await tip(iv)).statusAt) == naive(STAMP)


# --- needs-input -------------------------------------------------------------------------------------------


async def test_counts_have_every_need_even_when_empty(client):
    owner = await make_user()
    assert await needs(client, owner) == {"items": [], "counts": COUNTS}


async def test_outcome_fifteen_minutes_after_the_end(client):
    owner = await make_user()
    account = await Account(name="Backend", createdBy=owner.id).insert()
    due = await make_interview(owner, ended(timedelta(minutes=20)), company="Due", account=account)
    await make_interview(owner, ended(timedelta(minutes=10)), company="Just ended")
    await make_interview(owner, rnd(start=FUTURE), company="Ahead")
    out = await needs(client, owner)
    assert out["counts"] == {**COUNTS, "outcome": 1}
    entry = due.stageHistory[-1]
    assert out["items"] == [{
        "interviewId": str(due.id), "roundId": entry.id, "companyName": "Due", "profileName": "Backend",
        "ownerId": str(owner.id), "ownerName": "Pedro", "stage": "intro", "status": "scheduled",
        "scheduledAt": naive(entry.scheduledAt).isoformat() + "Z", "endsAt": naive(entry.endsAt).isoformat() + "Z",
        "need": "outcome",
    }]


@pytest.mark.parametrize("stage", ["ai_interview", "home_assessment"])
async def test_ai_interviews_and_home_assessments_get_no_step(client, stage):
    owner = await make_user()
    await make_interview(owner, ended(timedelta(minutes=20), stage=stage), company="Scheduled")
    await make_interview(owner, ended(timedelta(days=8), stage=stage, status="completed"), company="Held")
    await make_interview(
        owner, ended(timedelta(days=20), stage=stage, status="completed", followUps=followed(timedelta(days=8))),
        company="Followed up",
    )
    assert await needs(client, owner) == {"items": [], "counts": COUNTS}
    # …but a missing reason is still asked for.
    await make_interview(owner, ended(timedelta(days=1), stage=stage, status="canceled"), company="Canceled")
    assert [(i["companyName"], i["need"]) for i in (await needs(client, owner))["items"]] == [("Canceled", "reason")]


async def test_follow_up_seven_days_after_the_end(client):
    owner = await make_user()
    await make_interview(owner, ended(timedelta(days=3), status="completed"), company="Recent")
    await make_interview(owner, ended(timedelta(days=8), status="completed"), company="Overdue")
    out = await needs(client, owner)
    assert [(i["companyName"], i["need"], i["status"]) for i in out["items"]] == [
        ("Overdue", "follow_up", "completed"),
    ]
    assert "followedUpAt" not in out["items"][0]
    assert out["counts"] == {**COUNTS, "follow_up": 1}


async def test_heard_back_seven_days_after_the_follow_up(client):
    owner = await make_user()
    await make_interview(
        owner, ended(timedelta(days=10), status="completed", followUps=followed(timedelta(days=2))), company="Waiting",
    )
    asked = await make_interview(
        owner, ended(timedelta(days=16), status="completed", followUps=followed(timedelta(days=8), by=owner.id)),
        company="Asked",
    )
    out = await needs(client, owner)
    assert [(i["companyName"], i["need"]) for i in out["items"]] == [("Asked", "heard_back")]
    assert out["items"][0]["followedUpAt"] == naive(asked.stageHistory[-1].followUps[-1].at).isoformat() + "Z"
    assert out["counts"] == {**COUNTS, "heard_back": 1}


async def test_the_latest_follow_up_counts(client):
    owner = await make_user()
    twice = followed(timedelta(days=9)) + followed(timedelta(days=1))
    await make_interview(owner, ended(timedelta(days=20), status="completed", followUps=twice))
    assert (await needs(client, owner))["items"] == []


async def test_a_completed_round_followed_by_a_later_round_is_never_listed(client):
    owner = await make_user()
    later = rnd("final", status="scheduled", start=FUTURE)
    await make_interview(owner, ended(timedelta(days=8), status="completed"), later)
    await make_interview(
        owner, ended(timedelta(days=20), status="completed", followUps=followed(timedelta(days=8))), later,
    )
    assert await needs(client, owner) == {"items": [], "counts": COUNTS}


async def test_rounds_before_the_data_start_are_never_listed(client):
    owner = await make_user()
    await make_interview(owner, rnd(status="scheduled", start=OLD), company="Old outcome")
    await make_interview(owner, rnd(status="completed", start=OLD), company="Old follow-up")
    await make_interview(
        owner, rnd(status="completed", start=OLD, followUps=followed(timedelta(days=8))), company="Old heard back",
    )
    await make_interview(owner, rnd(status="canceled", start=OLD), company="Old reason")
    assert await needs(client, owner) == {"items": [], "counts": COUNTS}


async def test_every_need_newest_first_and_a_saved_row_leaves(client):
    owner = await make_user()
    await make_interview(owner, ended(timedelta(minutes=20)), company="Outcome")
    await make_interview(owner, ended(timedelta(days=1), status="rejected"), company="Reason")
    held = await make_interview(owner, ended(timedelta(days=8), status="completed"), company="Follow up")
    await make_interview(
        owner, ended(timedelta(days=16), status="completed", followUps=followed(timedelta(days=8))),
        company="Heard back",
    )
    out = await needs(client, owner)
    assert [(i["companyName"], i["need"]) for i in out["items"]] == [
        ("Outcome", "outcome"), ("Reason", "reason"), ("Follow up", "follow_up"), ("Heard back", "heard_back"),
    ]
    assert out["counts"] == {"outcome": 1, "follow_up": 1, "heard_back": 1, "reason": 1}
    for item in out["items"]:  # every need carries the round's end (start + 1 h here)
        start = datetime.fromisoformat(item["scheduledAt"].replace("Z", "+00:00"))
        assert item["endsAt"] == naive(start + timedelta(hours=1)).isoformat() + "Z"

    assert (await follow_up(client, owner, held)).status_code == 200
    out = await needs(client, owner)
    assert [i["companyName"] for i in out["items"]] == ["Outcome", "Reason", "Heard back"]
    assert out["counts"] == {"outcome": 1, "follow_up": 0, "heard_back": 1, "reason": 1}
