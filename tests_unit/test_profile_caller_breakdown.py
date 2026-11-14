"""Profiles list → Interviews column: caller-taken rounds broken down by caller (name and count), counted like the
caller total (held rounds; canceled rounds and home assessments left out) so the names add up to it (mongomock-motor)."""
import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")
os.environ["RESUME_QUEUE_DISABLED"] = "1"

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from beanie import init_beanie
from mongomock_motor import AsyncMongoMockClient

from app.models import (
    Account, Bid, Bidder, DailyPlan, Interview, InterviewQuestion, JobApplicationLog, ResumeApplication, UserExtra,
)

pytestmark = [
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]

CHI = ZoneInfo("America/Chicago")


def chi(month, day, hh=10) -> datetime:
    return datetime(2026, month, day, hh, tzinfo=CHI)


@pytest.fixture
async def db():
    await init_beanie(database=AsyncMongoMockClient()["profile-caller-breakdown-unit"], document_models=[
        UserExtra, Account, DailyPlan, Interview, InterviewQuestion, Bid, Bidder, JobApplicationLog, ResumeApplication])


@pytest.fixture
def today(monkeypatch):
    from app.routers import accounts

    monkeypatch.setattr(accounts, "_today", lambda: date(2026, 11, 13))


async def _iv(owner, acc, rounds):
    """One interview; `rounds`: (when, caller name or None for self, status, stage)."""
    history = [{"stage": stage, "at": w, "scheduledAt": w, "status": st,
                "caller": {"enabled": True, "callerName": name, "callerId": f"id-{name}"} if name else None}
               for w, name, st, stage in rounds]
    await Interview(accountId=acc.id, createdBy=owner.id, scheduledAt=rounds[-1][0], companyName="Acme",
                    stageHistory=history).insert()


async def _setup():
    ana = await UserExtra(name="Ana", email="ana@x.io").insert()
    a = await Account(name="A", region="US", createdBy=ana.id, createdAt=datetime(2026, 1, 5, 15)).insert()
    await _iv(ana, a, [(chi(11, 3), None, "completed", "intro"), (chi(11, 4), "Kim", "completed", "tech_round_1"),
                       (chi(11, 5), "Kim", "completed", "cultural")])
    await _iv(ana, a, [(chi(11, 6), "Bo", "completed", "intro"), (chi(11, 7), "Kim", "canceled", "tech_round_1")])
    await _iv(ana, a, [(chi(11, 6), "TBD", "scheduled", "intro")])          # caller not assigned yet
    await _iv(ana, a, [(chi(10, 20), "Zed", "completed", "intro")])         # before the range
    return ana


async def _list(user, **kw):
    from app.routers.accounts import list_profile_metrics

    return await list_profile_metrics(user, **{"userId": "", **kw})


async def test_interviews_by_caller_in_a_date_range(db, today):
    ana = await _setup()
    row = (await _list(ana, from_="2026-11-02", to="2026-11-08"))["rows"][0]
    assert row["callerInterviewsBy"] == [{"name": "Kim", "interviews": 2}, {"name": "Bo", "interviews": 1},
                                         {"name": "Caller not assigned", "interviews": 1}]
    assert sum(c["interviews"] for c in row["callerInterviewsBy"]) == row["callerInterviews"]
    assert row["selfInterviews"] == 1


async def test_interviews_by_caller_all_time(db, today):
    ana = await _setup()
    row = (await _list(ana, weeks="all"))["rows"][0]
    assert [(c["name"], c["interviews"]) for c in row["callerInterviewsBy"]] == [
        ("Kim", 2), ("Bo", 1), ("Zed", 1), ("Caller not assigned", 1)]
    assert sum(c["interviews"] for c in row["callerInterviewsBy"]) == row["callerInterviews"]


async def test_no_caller_interviews_is_an_empty_list(db, today):
    ana = await UserExtra(name="Ana", email="ana@x.io").insert()
    await Account(name="A", region="US", createdBy=ana.id, createdAt=datetime(2026, 1, 5, 15)).insert()
    row = (await _list(ana, from_="2026-11-02", to="2026-11-08"))["rows"][0]
    assert row["callerInterviewsBy"] == []
