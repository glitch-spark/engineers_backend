"""GET /accounts/{id}/metrics by period (week / month / custom / all, as the Statistics tabs): totals, day-by-day and
week-by-week counts and the owner's other profiles' average (in-memory MongoDB via mongomock-motor; route called
directly)."""
import os
from datetime import date, datetime

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")
os.environ["RESUME_QUEUE_DISABLED"] = "1"

import pytest
from beanie import init_beanie
from fastapi import HTTPException
from mongomock_motor import AsyncMongoMockClient

from app.models import (
    Account, Bid, Bidder, DailyPlan, Interview, InterviewQuestion, JobApplicationLog, ResumeApplication, UserExtra,
)
from app.routers import accounts as router

pytestmark = [
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]

TODAY = date(2026, 11, 4)  # a Wednesday


@pytest.fixture
async def db(monkeypatch):
    monkeypatch.setattr(router, "_today", lambda: TODAY)
    await init_beanie(
        database=AsyncMongoMockClient()["profile-metrics-range-unit"],
        document_models=[
            UserExtra, Account, DailyPlan, Interview, InterviewQuestion, Bid, Bidder, JobApplicationLog,
            ResumeApplication,
        ],
    )


async def _history():
    """Ana's profile A: self bids Oct 20 (5) and today (2), a bidder bid Oct 21, a caller round Oct 22, a tailored
    resume Oct 23 (score 80) and a self round Nov 2. Profile B: 4 self bids Oct 20."""
    ana = await UserExtra(name="ana", email="ana@x.io").insert()
    a = await Account(name="A", createdBy=ana.id, createdAt=datetime(2026, 1, 5, 15)).insert()
    b = await Account(name="B", createdBy=ana.id, createdAt=datetime(2026, 1, 5, 15)).insert()
    for day, by in [(date(2026, 10, 20), {str(a.id): 5, str(b.id): 4}), (TODAY, {str(a.id): 2})]:
        await DailyPlan(
            userId=ana.id, date=datetime(day.year, day.month, day.day), year=day.year, weekNumber=1,
            done={"bidsSelfByProfile": by}, loggedAt=datetime(day.year, day.month, day.day, 20),
        ).insert()
    bidder = await Bidder(createdBy=ana.id, name="Bo", profileId=a.id, rate=1, screenshotFolderUrl="s").insert()
    await Bid(bidderId=bidder.id, ownerId=ana.id, jobKey="k", firstAt=datetime(2026, 10, 21, 15), status="approved").insert()
    for stage, at, caller in [("intro", datetime(2026, 10, 22, 15), True), ("tech", datetime(2026, 11, 2, 15), False)]:
        await Interview(
            accountId=a.id, createdBy=ana.id, scheduledAt=at, companyName="Acme",
            stageHistory=[{"stage": stage, "at": at, "scheduledAt": at, "status": "completed",
                           "caller": {"enabled": True} if caller else None}],
        ).insert()
    await ResumeApplication(
        userId=ana.id, accountId=a.id, companyName="Acme", companyNameLower="acme", jobDescription="jd",
        status="completed", createdAt=datetime(2026, 10, 23, 15), matchScore={"total": 80, "band": "strong"},
    ).insert()
    return ana, a, b


async def _metrics(user, acc, period, day="", to=""):
    return await router.get_profile_metrics(str(acc.id), user, period=period, date=day, to=to)


async def test_month_totals_and_days(db):
    ana, a, _b = await _history()
    out = await _metrics(ana, a, "month", "2026-10-15")
    assert (out["start"], out["end"], out["label"]) == ("2026-10-01", "2026-10-31", "Oct 2026")
    assert len(out["daily"]) == 31 and out["daily"][0]["day"] == "2026-10-01"
    assert (out["bids"], out["selfBids"], out["bidderBids"]) == (6, 5, 1)
    assert (out["interviews"], out["selfInterviews"], out["callerInterviews"]) == (1, 0, 1)
    assert out["tailoredResumes"] == 1 and out["tailorScore"] == {"avg": 80.0, "n": 1}
    for k in ("selfBids", "bidderBids", "selfInterviews", "callerInterviews"):
        assert sum(d[k] for d in out["daily"]) == sum(w[k] for w in out["weekly"]) == out[k]


async def test_current_month_runs_to_today(db):
    ana, a, _b = await _history()
    out = await _metrics(ana, a, "month", "2026-11-04")
    assert (out["start"], out["end"]) == ("2026-11-01", "2026-11-04")
    assert len(out["daily"]) == 4
    assert (out["selfBids"], out["selfInterviews"]) == (2, 1)  # today's bids and Monday's round count


async def test_week_and_custom(db):
    ana, a, _b = await _history()
    week = await _metrics(ana, a, "week", "2026-10-22")
    assert (week["start"], week["end"], len(week["daily"])) == ("2026-10-19", "2026-10-25", 7)
    assert [w["week"] for w in week["weekly"]] == ["2026-10-19"]
    custom = await _metrics(ana, a, "custom", "2026-10-20", "2026-10-21")
    assert (custom["start"], custom["end"], custom["bids"]) == ("2026-10-20", "2026-10-21", 6)


async def test_all_time_starts_at_creation(db):
    ana, a, _b = await _history()
    out = await _metrics(ana, a, "all")
    assert (out["start"], out["end"], out["label"]) == ("2026-01-05", "2026-11-04", "All time")
    assert (out["bids"], out["interviews"], out["tailoredResumes"]) == (8, 2, 1)


async def test_peers_average_the_other_profiles_totals(db):
    ana, a, _b = await _history()
    out = await _metrics(ana, a, "month", "2026-10-15")
    assert out["peers"]["bids"] == 4 and out["peers"]["selfBids"] == 4 and out["peers"]["interviews"] == 0


async def test_bad_ranges_are_rejected(db):
    ana, a, _b = await _history()
    for period, day, to, code in [("year", "", "", 422), ("custom", "2026-10-20", "", 400),
                                  ("custom", "2026-10-21", "2026-10-20", 400), ("month", "Oct", "", 400)]:
        with pytest.raises(HTTPException) as e:
            await _metrics(ana, a, period, day, to)
        assert e.value.status_code == code, (period, day, to)


async def test_detail_access(db):
    ana, a, _b = await _history()
    ben = await UserExtra(name="ben", email="ben@x.io").insert()
    with pytest.raises(HTTPException) as e:
        await _metrics(ben, a, "month")
    assert e.value.status_code == 404
