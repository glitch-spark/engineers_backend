"""Round reasons (statusReason), the reschedule log and GET /interviews/needs-input. mongomock, Slack stubbed.
docs/superpowers/specs/2026-11-20-interview-reasons-design.md"""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from beanie import PydanticObjectId, init_beanie
from fastapi import HTTPException
from mongomock_motor import AsyncMongoMockClient

from app.interview_reasons import CANCEL_REASONS, REJECT_REASONS
from app.models import Account, UserExtra
from app.models.interview import Interview, InterviewStageEvent
from app.models.interview_question import InterviewQuestion
from app.routers.interviews import RoundIn, _build_round, flatten_rounds

pytestmark = [
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]

UTC = timezone.utc
START = datetime(2026, 10, 7, 15, 0, tzinfo=UTC)  # after the data start, long past
LATER = datetime(2026, 10, 9, 15, 0, tzinfo=UTC)
OLD = datetime(2026, 10, 5, 15, 0, tzinfo=UTC)  # before the data start (2026-10-06)
FUTURE = datetime(2099, 1, 5, 15, 0, tzinfo=UTC)


@pytest.fixture
async def db():
    database = AsyncMongoMockClient()["interview-reasons-unit"]
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


async def make_interview(owner, *rounds, company="Acme", account=None) -> Interview:
    rounds = rounds or (rnd(),)
    return await Interview(
        accountId=account.id if account else PydanticObjectId(), createdBy=owner.id,
        scheduledAt=rounds[-1]["scheduledAt"], companyName=company,
        stageHistory=list(rounds), stage=rounds[-1]["stage"], status=rounds[-1]["status"],
    ).insert()


async def patch(client, user, iv, body):
    async with client(user) as c:
        return await c.patch(f"/interviews/{iv.id}/stages/{iv.stageHistory[-1].id}", json=body)


async def tip(iv) -> InterviewStageEvent:
    return (await Interview.get(iv.id)).stageHistory[-1]


def naive(dt: datetime) -> datetime:
    return dt.astimezone(UTC).replace(tzinfo=None) if dt.tzinfo else dt


# --- the lists ----------------------------------------------------------------------------------------------


def test_reason_keys_are_the_contract():
    assert list(CANCEL_REASONS) == ["company", "me", "caller", "job_mismatch", "technical"]
    assert list(REJECT_REASONS) == [
        "no_reason", "no_answer", "location", "language", "technical_answers", "experience", "background", "test",
        "pay", "other_candidate", "av", "other",
    ]
    assert CANCEL_REASONS["job_mismatch"] == "Job doesn't match"
    assert REJECT_REASONS["no_answer"] == "No answer from the company"


# --- statusReason -------------------------------------------------------------------------------------------


async def test_cancel_with_a_reason(client):
    owner = await make_user()
    iv = await make_interview(owner)
    res = await patch(client, owner, iv, {"status": "canceled", "statusReason": "company", "statusNote": "moved"})
    assert res.status_code == 200, res.text
    saved = await tip(iv)
    assert (saved.status.value, saved.statusReason, saved.statusNote) == ("canceled", "company", "moved")
    out = res.json()["stageHistory"][-1]
    assert out["statusReason"] == "company" and out["reschedules"] == []


async def test_reject_with_a_reason(client):
    owner = await make_user()
    iv = await make_interview(owner, rnd(status="completed"))
    res = await patch(client, owner, iv, {"status": "rejected", "statusReason": "no_answer"})
    assert res.status_code == 200, res.text
    assert (await tip(iv)).statusReason == "no_answer"


async def test_reason_alone_on_a_canceled_round(client):
    owner = await make_user()
    iv = await make_interview(owner, rnd(status="canceled", statusNote="Role filled"))
    res = await patch(client, owner, iv, {"statusReason": "job_mismatch"})
    assert res.status_code == 200, res.text
    saved = await tip(iv)
    assert saved.statusReason == "job_mismatch" and saved.statusNote == "Role filled"


@pytest.mark.parametrize("body", [
    {"status": "canceled", "statusReason": "no_answer"},  # a rejected key
    {"status": "rejected", "statusReason": "company"},  # a canceled key
    {"status": "canceled", "statusReason": "nonsense"},
    {"status": "passed", "statusReason": "company"},  # takes none
    {"status": "completed", "statusReason": "other"},
    {"statusReason": "company"},  # the round stays scheduled
])
async def test_wrong_reason_is_400(client, body):
    owner = await make_user()
    iv = await make_interview(owner)
    res = await patch(client, owner, iv, body)
    assert res.status_code == 400 and res.json()["detail"] == "invalid statusReason"
    saved = await tip(iv)
    assert saved.status.value == "scheduled" and saved.statusReason is None


async def test_new_status_without_a_reason_clears_the_old_one(client):
    owner = await make_user()
    iv = await make_interview(owner, rnd(status="canceled", statusReason="company"))
    await patch(client, owner, iv, {"status": "scheduled"})
    saved = await tip(iv)
    assert saved.status.value == "scheduled" and saved.statusReason is None


async def test_canceled_to_rejected_does_not_keep_a_canceled_reason(client):
    owner = await make_user()
    iv = await make_interview(owner, rnd(status="canceled", statusReason="company"))
    res = await patch(client, owner, iv, {"status": "rejected"})
    assert res.status_code == 200
    assert (await tip(iv)).statusReason is None


async def test_other_edits_keep_the_reason_and_blank_clears_it(client):
    owner = await make_user()
    iv = await make_interview(owner, rnd(status="canceled", statusReason="company"))
    await patch(client, owner, iv, {"note": "prep", "status": "canceled"})
    assert (await tip(iv)).statusReason == "company"
    await patch(client, owner, iv, {"statusReason": ""})
    assert (await tip(iv)).statusReason is None


async def test_legacy_put_status_change_clears_the_reason(client):
    owner = await make_user()
    iv = await make_interview(owner, rnd(status="canceled", statusReason="company"))
    async with client(owner) as c:
        res = await c.put(f"/interviews/{iv.id}", json={"status": "scheduled"})
    assert res.status_code == 200, res.text
    assert (await tip(iv)).statusReason is None


async def test_build_round_takes_a_reason(db):
    owner = await make_user()
    entry = _build_round(
        RoundIn(stage="intro", scheduledAt=START.isoformat(), status="rejected", statusReason="pay"),
        user=owner, source="create",
    )
    assert entry.statusReason == "pay" and entry.reschedules == []
    for status, reason in (("scheduled", "pay"), ("rejected", "company"), ("canceled", "x")):
        with pytest.raises(HTTPException) as exc:
            _build_round(
                RoundIn(stage="intro", scheduledAt=START.isoformat(), status=status, statusReason=reason),
                user=owner, source="create",
            )
        assert exc.value.status_code == 400 and exc.value.detail == "invalid statusReason"


async def test_add_round_takes_a_reason(client):
    owner = await make_user()
    iv = await make_interview(owner, rnd(status="passed"))
    body = {"stage": "tech_round_1", "scheduledAt": LATER.isoformat(), "status": "canceled", "statusReason": "caller"}
    async with client(owner) as c:
        res = await c.post(f"/interviews/{iv.id}/stages", json=body)
        bad = await c.post(f"/interviews/{iv.id}/stages", json={**body, "stage": "final", "statusReason": "pay"})
    assert res.status_code == 201, res.text
    assert res.json()["stageHistory"][-1]["statusReason"] == "caller"
    assert bad.status_code == 400 and bad.json()["detail"] == "invalid statusReason"
    assert len((await Interview.get(iv.id)).stageHistory) == 2


def test_calendar_rows_carry_the_reason():
    row = {
        "_id": "iv1", "companyName": "Acme",
        "stageHistory": [{"id": "r1", "stage": "intro", "status": "canceled", "scheduledAt": START,
                          "statusReason": "company"}],
    }
    [out] = flatten_rounds([row], from_dt=None, to_dt=None)
    assert out["statusReason"] == "company"
    row["stageHistory"][0].pop("statusReason")
    [out] = flatten_rounds([row], from_dt=None, to_dt=None)
    assert out["statusReason"] is None


async def test_manager_may_send_the_reasons(client):
    owner, puma = await make_user(), await make_user("Puma", role="interview_manager")
    iv = await make_interview(owner)
    res = await patch(client, puma, iv, {
        "scheduledAt": LATER.isoformat(), "rescheduleReason": "company", "rescheduleNote": "they moved it",
    })
    assert res.status_code == 200, res.text
    res = await patch(client, puma, iv, {"status": "canceled", "statusReason": "technical"})
    assert res.status_code == 200, res.text
    saved = await tip(iv)
    assert saved.statusReason == "technical" and saved.reschedules[0].by == puma.id


# --- reschedules --------------------------------------------------------------------------------------------


async def test_reschedule_with_a_reason_is_logged(client):
    owner = await make_user()
    iv = await make_interview(owner)
    res = await patch(client, owner, iv, {
        "scheduledAt": LATER.isoformat(), "rescheduleReason": "caller", "rescheduleNote": "  sick  ",
    })
    assert res.status_code == 200, res.text
    saved = await tip(iv)
    assert naive(saved.scheduledAt) == naive(LATER)
    [log] = saved.reschedules
    assert (naive(log.fromAt), naive(log.toAt), log.reason, log.note, log.by) == (
        naive(START), naive(LATER), "caller", "sick", owner.id,
    )
    assert log.at is not None
    out = res.json()["stageHistory"][-1]["reschedules"]
    assert out == [{
        "at": out[0]["at"], "by": str(owner.id), "fromAt": "2026-10-07T15:00:00Z", "toAt": "2026-10-09T15:00:00Z",
        "reason": "caller", "note": "sick",
    }]
    assert out[0]["at"].endswith("Z")


async def test_two_reschedules_are_two_entries(client):
    owner = await make_user()
    iv = await make_interview(owner)
    third = LATER + timedelta(days=3)
    await patch(client, owner, iv, {"scheduledAt": LATER.isoformat(), "rescheduleReason": "company"})
    res = await patch(client, owner, iv, {"scheduledAt": third.isoformat(), "rescheduleReason": "technical"})
    assert res.status_code == 200, res.text
    logs = (await tip(iv)).reschedules
    assert [(naive(r.fromAt), naive(r.toAt), r.reason) for r in logs] == [
        (naive(START), naive(LATER), "company"), (naive(LATER), naive(third), "technical"),
    ]


async def test_time_change_without_a_reason_logs_nothing(client):
    owner = await make_user()
    iv = await make_interview(owner)
    res = await patch(client, owner, iv, {"scheduledAt": LATER.isoformat()})
    assert res.status_code == 200, res.text
    saved = await tip(iv)
    assert naive(saved.scheduledAt) == naive(LATER) and saved.reschedules == []


@pytest.mark.parametrize("body", [
    {"rescheduleReason": "company"},  # no time
    {"rescheduleReason": "company", "scheduledAt": START.isoformat()},  # the same time
    {"rescheduleReason": "company", "scheduledAt": "2026-10-07T11:00:00-04:00"},  # the same instant
])
async def test_reschedule_reason_without_a_time_change_is_400(client, body):
    owner = await make_user()
    iv = await make_interview(owner)
    res = await patch(client, owner, iv, body)
    assert res.status_code == 400, res.text
    assert (await tip(iv)).reschedules == []


@pytest.mark.parametrize("reason", ["nonsense", "no_answer"])
async def test_unknown_reschedule_reason_is_400(client, reason):
    owner = await make_user()
    iv = await make_interview(owner)
    res = await patch(client, owner, iv, {"scheduledAt": LATER.isoformat(), "rescheduleReason": reason})
    assert res.status_code == 400 and res.json()["detail"] == "invalid rescheduleReason"
    saved = await tip(iv)
    assert naive(saved.scheduledAt) == naive(START) and saved.reschedules == []


# --- needs-input --------------------------------------------------------------------------------------------


async def needs(client, user, **params) -> dict:
    async with client(user) as c:
        res = await c.get("/interviews/needs-input", params=params)
    assert res.status_code == 200, res.text
    return res.json()


async def test_needs_input_lists_results_and_reasons_newest_first(client):
    owner = await make_user()
    account = await Account(name="Backend", createdBy=owner.id).insert()
    held = await make_interview(owner, rnd(status="completed"), company="Held", account=account)
    canceled = await make_interview(owner, rnd(status="canceled", start=LATER), company="Canceled")
    rejected = await make_interview(
        owner, rnd(status="rejected"), rnd("final", status="scheduled", start=FUTURE), company="Rejected",
    )
    await make_interview(owner, rnd(status="canceled", statusReason="company"), company="Has a reason")
    await make_interview(owner, rnd(status="passed"), company="Done")
    await make_interview(owner, rnd(status="scheduled", start=FUTURE), company="Not held yet")

    out = await needs(client, owner)
    assert out["counts"] == {"outcome": 0, "follow_up": 1, "heard_back": 0, "reason": 2}
    assert [(i["companyName"], i["need"], i["status"]) for i in out["items"]] == [
        ("Canceled", "reason", "canceled"), ("Held", "follow_up", "completed"), ("Rejected", "reason", "rejected"),
    ]
    assert out["items"][1] == {
        "interviewId": str(held.id), "roundId": held.stageHistory[-1].id, "companyName": "Held",
        "profileName": "Backend", "ownerId": str(owner.id), "ownerName": "Pedro", "stage": "intro",
        "status": "completed", "scheduledAt": "2026-10-07T15:00:00Z", "endsAt": "2026-10-07T16:00:00Z",
        "need": "follow_up",
    }
    assert out["items"][0]["interviewId"] == str(canceled.id) and out["items"][0]["profileName"] is None
    assert out["items"][2]["roundId"] == rejected.stageHistory[0].id


async def test_completed_round_followed_by_a_later_round_is_not_listed(client):
    owner = await make_user()
    await make_interview(owner, rnd(status="completed"), rnd("final", status="scheduled", start=FUTURE))
    assert (await needs(client, owner)) == {
        "items": [], "counts": {"outcome": 0, "follow_up": 0, "heard_back": 0, "reason": 0},
    }


async def test_completed_round_that_has_not_ended_is_not_listed(client):
    owner = await make_user()
    await make_interview(owner, rnd(status="completed", start=FUTURE))
    assert (await needs(client, owner))["items"] == []


async def test_completed_round_waits_seven_days_for_a_result(client):
    """The company may not have answered yet: a held round asks for its result only once it ended 7 days ago."""
    owner = await make_user()
    now = datetime.now(UTC).replace(microsecond=0)
    await make_interview(owner, rnd(status="completed", start=now - timedelta(days=3)), company="Recent")
    await make_interview(owner, rnd(status="completed", start=now - timedelta(days=8)), company="Overdue")
    await make_interview(owner, rnd(status="canceled", start=now - timedelta(days=1)), company="Canceled")  # no wait
    out = await needs(client, owner)
    assert [i["companyName"] for i in out["items"]] == ["Canceled", "Overdue"]
    assert (out["counts"]["follow_up"], out["counts"]["reason"]) == (1, 1)


async def test_rounds_before_the_data_start_are_not_listed(client):
    owner = await make_user()
    await make_interview(owner, rnd(status="completed", start=OLD), company="Old result")
    await make_interview(owner, rnd(status="canceled", start=OLD), company="Old reason")
    # An old canceled round in an interview that also has a recent round: still not listed.
    await make_interview(owner, rnd(status="canceled", start=OLD), rnd("final", status="passed"), company="Mixed")
    assert (await needs(client, owner))["items"] == []


async def test_data_start_is_local_midnight_in_the_app_timezone(client):
    # 2026-10-06 00:00 in New York is 04:00 UTC.
    owner = await make_user()
    await make_interview(owner, rnd(status="canceled", start=datetime(2026, 10, 6, 3, 0, tzinfo=UTC)), company="Before")
    await make_interview(owner, rnd(status="canceled", start=datetime(2026, 10, 6, 5, 0, tzinfo=UTC)), company="After")
    assert [i["companyName"] for i in (await needs(client, owner))["items"]] == ["After"]


@pytest.mark.parametrize("stage", ["ai_interview", "home_assessment"])
async def test_ai_interviews_and_home_assessments_are_not_results(client, stage):
    owner = await make_user()
    await make_interview(owner, rnd(stage, status="completed"))
    assert (await needs(client, owner))["items"] == []


async def test_a_normal_user_sees_only_their_own(client):
    pedro, sam = await make_user(), await make_user("Sam")
    await make_interview(pedro, rnd(status="completed"), company="Pedro's")
    await make_interview(sam, rnd(status="completed"), company="Sam's")
    assert [i["companyName"] for i in (await needs(client, pedro))["items"]] == ["Pedro's"]
    # Asking for someone else's changes nothing.
    assert [i["companyName"] for i in (await needs(client, pedro, userId=str(sam.id)))["items"]] == ["Pedro's"]
    manager = await make_user("Puma", role="interview_manager")
    assert (await needs(client, manager, userId=str(sam.id)))["items"] == []


async def test_admin_sees_everyone_or_one_person(client):
    pedro, sam, admin = await make_user(), await make_user("Sam"), await make_user("Root", role="admin")
    await make_interview(pedro, rnd(status="completed"), company="Pedro's")
    await make_interview(sam, rnd(status="canceled", start=LATER), company="Sam's")
    everyone = await needs(client, admin)
    assert [(i["companyName"], i["ownerName"]) for i in everyone["items"]] == [("Sam's", "Sam"), ("Pedro's", "Pedro")]
    assert everyone["counts"] == {"outcome": 0, "follow_up": 1, "heard_back": 0, "reason": 1}
    one = await needs(client, admin, userId=str(sam.id))
    assert [(i["companyName"], i["ownerId"]) for i in one["items"]] == [("Sam's", str(sam.id))]
    assert one["counts"] == {"outcome": 0, "follow_up": 0, "heard_back": 0, "reason": 1}
    async with client(admin) as c:
        assert (await c.get("/interviews/needs-input", params={"userId": "nope"})).status_code == 400


async def test_saving_the_reason_removes_the_row(client):
    owner = await make_user()
    iv = await make_interview(owner, rnd(status="completed"))
    assert len((await needs(client, owner))["items"]) == 1
    await patch(client, owner, iv, {"status": "rejected", "statusReason": "no_answer"})
    assert (await needs(client, owner))["items"] == []
