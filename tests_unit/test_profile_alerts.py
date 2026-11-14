"""Profiles list → banners: profiles with no interview in the last 14 days (red) and no applies, self or bidder, in
the last 21 days (grey, never also red); too-new profiles never flagged; all users' 14-day tailor average only from the
cache (mongomock-motor)."""
import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")
os.environ["RESUME_QUEUE_DISABLED"] = "1"

import itertools
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from beanie import init_beanie
from mongomock_motor import AsyncMongoMockClient

from app import profile_compare as pc
from app.models import (
    Account, Bid, Bidder, DailyPlan, Interview, InterviewQuestion, JobApplicationLog, ResumeApplication, UserExtra,
)

pytestmark = [
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]

CHI = ZoneInfo("America/Chicago")
_job = itertools.count()
NOV = {"from_": "2026-11-01", "to": "2026-11-30"}


@pytest.fixture
async def db():
    await init_beanie(database=AsyncMongoMockClient()["profile-alerts-unit"], document_models=[
        UserExtra, Account, DailyPlan, Interview, InterviewQuestion, Bid, Bidder, JobApplicationLog, ResumeApplication])


@pytest.fixture
def today(monkeypatch):
    from app.routers import accounts

    monkeypatch.setattr(accounts, "_today", lambda: date(2026, 11, 14))
    pc.benchmark_cache.clear()
    yield
    pc.benchmark_cache.clear()


async def _user(name, role="staff"):
    return await UserExtra(name=name, email=f"{name}@x.io", role=role).insert()


async def _profile(owner, name, created=datetime(2026, 10, 1, 15)):
    return await Account(name=name, region="US", createdBy=owner.id, createdAt=created).insert()


async def _bids(owner, acc, day: date, n):
    b = await Bidder(createdBy=owner.id, name=f"B{next(_job)}", profileId=acc.id, rate=1,
                     screenshotFolderUrl="s3://b/").insert()
    first = datetime(day.year, day.month, day.day, 10, tzinfo=CHI).astimezone(timezone.utc).replace(tzinfo=None)
    for _ in range(n):
        await Bid(bidderId=b.id, ownerId=owner.id, jobKey=f"k{next(_job)}", firstAt=first, status="approved",
                  screenshots=[]).insert()


async def _self_bids(owner, acc, day: date, n):
    await DailyPlan(userId=owner.id, date=datetime(day.year, day.month, day.day), year=day.year, weekNumber=1,
                    done={"bidsSelfByProfile": {str(acc.id): n}},
                    loggedAt=datetime(day.year, day.month, day.day, 20)).insert()


async def _round(owner, acc, day: date):
    when = datetime(day.year, day.month, day.day, 15)
    await Interview(accountId=acc.id, createdBy=owner.id, scheduledAt=when, companyName="Acme",
                    stageHistory=[{"stage": "intro", "at": when, "scheduledAt": when, "status": "passed"}]).insert()


async def _tailored(acc, day: date, score):
    await ResumeApplication.get_motor_collection().insert_one({
        "accountId": acc.id, "status": "completed", "fixedFrom": None,
        "createdAt": datetime(day.year, day.month, day.day, 16), "matchScore": {"total": score}})


async def _setup():
    ana = await _user("ana")
    v = await _profile(ana, "V")
    await _bids(ana, v, date(2026, 11, 10), 379)
    await _round(ana, v, date(2026, 10, 30))                 # before the 14 days
    await _tailored(v, date(2026, 11, 10), 60)
    await _tailored(v, date(2026, 11, 11), 70)
    d = await _profile(ana, "D")
    await _bids(ana, d, date(2026, 11, 10), 50)
    await _round(ana, d, date(2026, 11, 1))                  # = today - 13: counts
    t = await _profile(ana, "T")
    await _bids(ana, t, date(2026, 10, 23), 5)              # today - 22: outside the 21 days
    u = await _profile(ana, "U")
    await _bids(ana, u, date(2026, 10, 25), 5)              # = today - 20: inside
    s = await _profile(ana, "S")
    await _self_bids(ana, s, date(2026, 11, 3), 20)          # self bids are applies too
    await _profile(ana, "New14", created=datetime(2026, 11, 2, 15))   # after today - 13: never flagged
    await _profile(ana, "New21", created=datetime(2026, 10, 26, 15))  # old enough for red, too new for grey
    return ana


async def _alerts(user, **kw):
    from app.routers.accounts import list_profile_metrics

    return (await list_profile_metrics(user, **{"userId": "", **NOV, **kw}))["alerts"]


async def test_profiles_with_no_interviews_in_14_days_are_red(db, today):
    al = await _alerts(await _setup())
    assert al["asOf"] == "2026-11-14"
    assert [x["name"] for x in al["noInterviews"]] == ["V", "S", "New21", "U"]   # most bids first, then name
    v = al["noInterviews"][0]
    assert (v["bids"], v["tailored"], v["tailorScore"]) == (379, 2, {"avg": 65.0, "n": 2})


async def test_no_applies_in_21_days_is_grey_not_red(db, today):
    al = await _alerts(await _setup())
    assert [x["name"] for x in al["noApplies"]] == ["T"]
    assert "T" not in [x["name"] for x in al["noInterviews"]]


async def test_owner_with_only_new_profiles_gets_empty_alerts(db, today):
    bo = await _user("bo")
    await _profile(bo, "Fresh", created=datetime(2026, 11, 10, 15))
    al = await _alerts(bo)
    assert al["noInterviews"] == [] and al["noApplies"] == []


async def test_all_users_tailor_average_comes_from_the_cache_or_is_null(db, today):
    ana = await _setup()
    assert (await _alerts(ana))["allTailorScore"] is None           # cold cache: never waits
    await pc.benchmark_cache.wait_refreshes()
    assert (await _alerts(ana))["allTailorScore"] == pytest.approx(65.0)


async def test_admin_viewing_everyone_gets_no_alerts(db, today):
    await _setup()
    assert await _alerts(await _user("boss", role="admin")) is None
