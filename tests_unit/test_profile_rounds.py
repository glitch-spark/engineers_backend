"""Profile → Metrics: interviews by round type with pass rates, home assessments included as their own type
(in-memory MongoDB via mongomock-motor). Same rules as Statistics → Callers."""
import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")
os.environ["RESUME_QUEUE_DISABLED"] = "1"

from datetime import date, datetime
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

NOV = (date(2026, 11, 1), date(2026, 11, 13))


def at(month, day, hour=15) -> datetime:
    """15:00 UTC: the same local day in any app time zone from UTC-12 to UTC+8."""
    return datetime(2026, month, day, hour)


@pytest.fixture
async def db():
    await init_beanie(database=AsyncMongoMockClient()["profile-rounds-unit"], document_models=[
        UserExtra, Account, DailyPlan, Interview, InterviewQuestion, Bid, Bidder, JobApplicationLog, ResumeApplication])


@pytest.fixture
def today(monkeypatch):
    from app.routers import accounts

    monkeypatch.setattr(accounts, "_today", lambda: date(2026, 11, 13))


async def _owner_profile():
    ana = await UserExtra(name="Ana", email="ana@x.io").insert()
    a = await Account(name="A", region="US", createdBy=ana.id, createdAt=datetime(2026, 1, 5, 15)).insert()
    return ana, a


async def _iv(owner, acc, entries):
    """One interview whose history is these (stage, when, status) entries, in order."""
    history = [{"stage": s, "at": w, "scheduledAt": w, "status": st} for s, w, st in entries]
    await Interview(accountId=acc.id, createdBy=owner.id, scheduledAt=entries[-1][1], companyName="Acme",
                    stageHistory=history).insert()


async def _summary(acc):
    return pm.rounds_summary((await pm.profile_rounds([acc.id], *NOV)).get(str(acc.id), []))


async def test_rounds_by_type_with_pass_rates(db):
    ana, a = await _owner_profile()
    await _iv(ana, a, [("intro", at(11, 3), "passed"), ("tech_round_1", at(11, 5), "rejected")])
    await _iv(ana, a, [("intro", at(11, 4), "rejected")])
    await _iv(ana, a, [("intro", at(11, 6), "completed"), ("live_coding", at(11, 7), "passed")])
    await _iv(ana, a, [("cultural", at(11, 8), "scheduled")])
    await _iv(ana, a, [("final", at(11, 9), "canceled")])
    await _iv(ana, a, [("ai_interview", at(11, 9), "passed")])
    await _iv(ana, a, [("home_assessment", at(11, 10), "passed"), ("intro", at(10, 1), "passed")])  # Oct 1: outside
    s = await _summary(a)
    assert s["byType"]["intro"] == {"done": 3, "passed": 1, "rejected": 1, "passRate": 0.5}
    assert s["byType"]["tech"] == {"done": 2, "passed": 1, "rejected": 1, "passRate": 0.5}
    assert s["byType"]["hiring"] == {"done": 0, "passed": 0, "rejected": 0, "passRate": None}
    assert s["byType"]["final"]["done"] == 0 and s["byType"]["panel"]["passRate"] is None
    assert s["byType"]["home_assessment"] == {"done": 1, "passed": 1, "rejected": 0, "passRate": 1.0}
    assert list(s["byType"]) == list(pm.ROUND_TYPES)
    assert (s["awaiting"], s["scheduled"]) == (1, 1)
    assert s["passRate"] == 0.5  # 2 passed / 4 decided; the home assessment is left out


async def test_no_rounds_has_no_rates(db):
    _ana, a = await _owner_profile()
    s = await _summary(a)
    assert s["passRate"] is None and all(t["passRate"] is None and t["done"] == 0 for t in s["byType"].values())


async def test_same_stage_entries_are_one_round(db):
    ana, a = await _owner_profile()
    await _iv(ana, a, [("intro", at(11, 3), "scheduled"), ("intro", at(11, 4), "scheduled"),
                       ("intro", at(11, 5), "passed")])
    s = await _summary(a)
    assert s["byType"]["intro"] == {"done": 1, "passed": 1, "rejected": 0, "passRate": 1.0}
    assert s["scheduled"] == 0


async def test_home_assessment_dated_by_interview_when_no_history(db):
    ana, a = await _owner_profile()
    await Interview(accountId=a.id, createdBy=ana.id, companyName="Acme", scheduledAt=at(11, 6),
                    stage="home_assessment", status="completed").insert()
    s = await _summary(a)
    assert s["byType"]["home_assessment"]["done"] == 1
    assert s["awaiting"] == 1


async def test_edge_day_in_local_time(db, monkeypatch):
    monkeypatch.setattr(pm, "app_tz", lambda: ZoneInfo("America/Chicago"))
    ana, a = await _owner_profile()
    await _iv(ana, a, [("intro", datetime(2026, 11, 14, 5, 30), "passed")])  # Nov 13 23:30 in Chicago
    await _iv(ana, a, [("tech_round_1", datetime(2026, 11, 1, 4, 30), "passed")])  # Oct 31 23:30 in Chicago
    s = await _summary(a)
    assert s["byType"]["intro"]["done"] == 1
    assert s["byType"]["tech"]["done"] == 0


async def test_profile_metrics_endpoint_returns_rounds_and_peer_rate(db, today):
    ana, a = await _owner_profile()
    b = await Account(name="B", region="US", createdBy=ana.id, createdAt=datetime(2026, 1, 5, 15)).insert()
    await _iv(ana, a, [("intro", at(11, 3), "passed")])
    await _iv(ana, b, [("intro", at(11, 3), "passed"), ("tech_round_1", at(11, 4), "rejected"),
                       ("final", at(11, 5), "rejected")])
    from app.routers.accounts import get_profile_metrics

    out = await get_profile_metrics(str(a.id), ana, period="month", date="2026-11-13", to="")
    assert out["rounds"]["byType"]["intro"] == {"done": 1, "passed": 1, "rejected": 0, "passRate": 1.0}
    assert out["passRate"] == 1.0
    assert out["peers"]["passRate"] == 0.3333
