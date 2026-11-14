"""Profiles list → card: the owner's top profile per region (most interviews, best interview rate with 30+ bids) and
all users' best numbers per region in the benchmark, numbers only (mongomock-motor)."""
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
from app import profile_metrics as pm
from app.config import get_settings
from app.models import (
    Account, Bid, Bidder, DailyPlan, Interview, InterviewQuestion, JobApplicationLog, ResumeApplication, UserExtra,
)

pytestmark = [
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]

_job = itertools.count()
NOV = {"from_": "2026-11-01", "to": "2026-11-13"}


@pytest.fixture
async def db():
    await init_beanie(database=AsyncMongoMockClient()["profiles-region-tops-unit"], document_models=[
        UserExtra, Account, DailyPlan, Interview, InterviewQuestion, Bid, Bidder, JobApplicationLog, ResumeApplication])


@pytest.fixture
def today(monkeypatch):
    from app.routers import accounts

    monkeypatch.setattr(accounts, "_today", lambda: date(2026, 11, 13))
    pc.benchmark_cache.clear()
    yield
    pc.benchmark_cache.clear()


def launch(monkeypatch, day: str):
    monkeypatch.setattr(pm, "get_settings", lambda: get_settings().model_copy(update={"PROFILE_METRICS_SINCE": day}))


async def _user(name, role="staff"):
    return await UserExtra(name=name, email=f"{name}@x.io", role=role).insert()


async def _profile(owner, name, created=datetime(2026, 1, 5, 15), region="US", **kw):
    return await Account(name=name, region=region, createdBy=owner.id, createdAt=created, **kw).insert()


async def _bids(owner, acc, day, n):
    b = await Bidder(createdBy=owner.id, name=f"B{next(_job)}", profileId=acc.id, rate=1,
                     screenshotFolderUrl="s3://b/").insert()
    first = datetime(2026, 11, day, 10, tzinfo=ZoneInfo("America/Chicago")).astimezone(timezone.utc).replace(tzinfo=None)
    for _ in range(n):
        await Bid(bidderId=b.id, ownerId=owner.id, jobKey=f"k{next(_job)}", firstAt=first, status="approved",
                  screenshots=[]).insert()


async def _round(owner, acc, day, status="passed"):
    when = datetime(2026, 11, day, 15)
    await Interview(accountId=acc.id, createdBy=owner.id, scheduledAt=when, companyName="Acme",
                    stageHistory=[{"stage": "intro", "at": when, "scheduledAt": when, "status": status}]).insert()


async def _list(user, userId="", **kw):
    from app.routers.accounts import list_profile_metrics

    return await list_profile_metrics(user, userId=userId, **kw)


async def _p(owner, name, bids, interviews, region="US"):
    acc = await _profile(owner, name, region=region)
    if bids:
        await _bids(owner, acc, 3, bids)
    for _ in range(interviews):
        await _round(owner, acc, 4)
    return acc


async def test_region_tops_pick_most_interviews_and_best_rate(db, today):
    ana = await _user("ana")
    a = await _p(ana, "A", 40, 2)
    b = await _p(ana, "B", 100, 3)
    await _p(ana, "C", 10, 1)                       # best rate on paper, but under 30 bids
    e = await _p(ana, "E", 50, 0, region="EU")
    await _p(ana, "N", 60, 5, region=None)          # no region: never in the tops
    await _p(ana, "K", 60, 5, region="Canada")      # not a shown region
    out = await _list(ana, from_="2026-11-01", to="2026-11-30")
    us = out["regionTops"]["US"]
    assert us["mostInterviews"] == {"accountId": str(b.id), "name": "B", "interviews": 3}
    assert us["bestRate"] == {"accountId": str(a.id), "name": "A", "interviewsPerBid": 0.05, "bids": 40}
    assert out["regionTops"]["EU"] == {
        "mostInterviews": None, "bestRate": {"accountId": str(e.id), "name": "E", "interviewsPerBid": 0.0, "bids": 50}}
    assert set(out["regionTops"]) == {"US", "EU"}   # no Latam profile


async def test_region_top_ties(db, today):
    ana = await _user("ana")
    await _p(ana, "X", 40, 2)    # 0.05
    await _p(ana, "Y", 80, 4)    # 0.05, more bids; 4 interviews
    await _p(ana, "Z", 100, 4)   # 0.04; 4 interviews
    us = (await _list(ana, from_="2026-11-01", to="2026-11-30"))["regionTops"]["US"]
    assert us["mostInterviews"]["name"] == "Y"      # 4 like Z, better rate
    assert us["bestRate"]["name"] == "Y"            # 0.05 like X, more bids


async def test_benchmark_region_tops_are_numbers_only(db, today):
    bo, cy = await _user("bo"), await _user("cy")
    await _p(bo, "P1", 300, 9)
    await _p(cy, "P2", 100, 3)
    await _p(cy, "Q", 20, 0, region="EU")
    bench = await pc.benchmark((date(2026, 11, 1), date(2026, 11, 13)))
    assert bench["regionTops"] == {"US": {"mostInterviews": 9, "bestRate": 0.03},
                                   "EU": {"mostInterviews": None, "bestRate": None}}


async def test_admin_viewing_everyone_gets_no_region_tops(db, today):
    ana, boss = await _user("ana"), await _user("boss", role="admin")
    await _p(ana, "A", 40, 2)
    assert (await _list(boss, from_="2026-11-01", to="2026-11-30"))["regionTops"] is None
