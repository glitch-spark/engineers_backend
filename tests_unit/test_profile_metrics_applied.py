"""Profiles list: interviews split by who applied (self / bidder / not set), first and last apply dates, and the self
and bidder rates from Nov 6 (in-memory MongoDB via mongomock-motor)."""
import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")
os.environ["RESUME_QUEUE_DISABLED"] = "1"

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from beanie import init_beanie
from mongomock_motor import AsyncMongoMockClient

from app import profile_metrics as pm
from app.models import (
    Account, Bid, Bidder, DailyPlan, Interview, InterviewQuestion, JobApplicationLog, ResumeApplication, UserExtra,
)

pytestmark = [
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]

CHI = ZoneInfo("America/Chicago")


def at(month, day, hour=15) -> datetime:
    """15:00 UTC: the same local day in any app time zone from UTC-12 to UTC+8."""
    return datetime(2026, month, day, hour)


def chi(month, day, hh, mm=0) -> datetime:
    return datetime(2026, month, day, hh, mm, tzinfo=CHI)


@pytest.fixture
async def db():
    await init_beanie(database=AsyncMongoMockClient()["profile-applied-unit"], document_models=[
        UserExtra, Account, DailyPlan, Interview, InterviewQuestion, Bid, Bidder, JobApplicationLog, ResumeApplication])


async def _owner_profile():
    ana = await UserExtra(name="Ana", email="ana@x.io").insert()
    a = await Account(name="A", region="US", createdBy=ana.id, createdAt=datetime(2026, 1, 5, 15)).insert()
    return ana, a


async def _iv(owner, acc, *, applied, rounds, caller=False):
    """One interview: a completed round per date (stages intro, tech_round_1, cultural…); the last takes a caller."""
    stages = ["intro", "tech_round_1", "cultural", "final"]
    history = [{"stage": stages[i], "at": w, "scheduledAt": w, "status": "completed",
                "caller": {"enabled": True} if caller and i == len(rounds) - 1 else None}
               for i, w in enumerate(rounds)]
    await Interview(accountId=acc.id, createdBy=owner.id, scheduledAt=rounds[-1], companyName="Acme",
                    stageHistory=history, appliedBy=applied).insert()


async def test_rounds_split_by_who_applied(db):
    ana, a = await _owner_profile()
    await _iv(ana, a, applied={"kind": "self"}, rounds=[at(11, 5), at(11, 7)])
    await _iv(ana, a, applied={"kind": "bidder", "bidderName": "Bo"}, rounds=[at(11, 7)], caller=True)
    await _iv(ana, a, applied=None, rounds=[at(11, 9)])
    out = await pm.collect_range([a], date(2026, 11, 2), date(2026, 11, 9))
    row = pm.summarize(str(a.id), *out[str(a.id)])
    assert (row["interviews"], row["selfAppliedInterviews"], row["bidderAppliedInterviews"],
            row["unsetAppliedInterviews"]) == (4, 2, 1, 1)
    assert (row["selfInterviews"], row["callerInterviews"]) == (3, 1)  # who took them, unchanged
