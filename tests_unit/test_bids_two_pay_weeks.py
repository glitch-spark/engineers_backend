"""Bidders page, Last 2 pay weeks: `weeks=2` with `period=week` covers the anchored pay week and the one before, a
column per pay week (as a custom range), compared with the 2 pay weeks before (mongomock-motor)."""
import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

import itertools
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from beanie import init_beanie
from mongomock_motor import AsyncMongoMockClient

from app.bids import insights as ins
from app.models import Account, UserExtra
from app.models.bid import Bid
from app.models.bidder import Bidder
from app.models.daily_plan import DailyPlan
from app.models.interview import Interview
from app.models.interview_question import InterviewQuestion
from app.routers import bids as bids_router

pytestmark = [
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]

CHI = ZoneInfo("America/Chicago")
_n = itertools.count()


@pytest.fixture
async def db(monkeypatch):
    from app.bids import records
    from app.config import get_settings

    database = AsyncMongoMockClient()["bids-two-weeks-unit"]
    await init_beanie(database=database, document_models=[
        UserExtra, Account, Bidder, Bid, DailyPlan, Interview, InterviewQuestion])
    settings = get_settings().model_copy(update={"BID_RECORDS_START": None})
    monkeypatch.setattr(records, "get_settings", lambda: settings)
    ins.platform_cache.clear()
    yield database
    ins.platform_cache.clear()


def at(monkeypatch, *args):
    now = datetime(*args, tzinfo=CHI).astimezone(timezone.utc)
    monkeypatch.setattr(bids_router, "_now", lambda: now)


async def setup():
    pedro = await UserExtra(name="Pedro", email="pedro@x.io", role="staff").insert()
    ana = await Bidder(createdBy=pedro.id, name="Ana", rate=0.5, folder="ana", screenshotFolderUrl="s3://shots/ana/",
                       createdAt=datetime(2026, 1, 1)).insert()
    return pedro, ana


async def bid(b, *chicago, n=1):
    first = datetime(*chicago, tzinfo=CHI).astimezone(timezone.utc).replace(tzinfo=None)
    for _ in range(n):
        await Bid(bidderId=b.id, ownerId=b.createdBy, jobKey=f"ana/x/j{next(_n)}", jobUrl="https://j.example/x",
                  jobTitle="x", firstAt=first, status="approved", screenshots=[]).insert()


async def perf(u, **kw):
    return await bids_router.bids_performance(
        u, **{"period": "week", "date": "", "to": "", "weeks": 2, "includeArchived": False, "ownerId": None, **kw})


async def test_two_pay_weeks_start_with_the_previous_pay_week_and_end_today(db, monkeypatch):
    at(monkeypatch, 2026, 11, 18, 9)                  # Wed of the pay week Sun Nov 15 .. Sat Nov 21
    pedro, _ana = await setup()
    r = await perf(pedro)
    assert (r["start"], r["end"]) == ("2026-11-08", "2026-11-18")
    assert r["label"] == "Nov 8–21, 2026"
    assert r["period"] == "week" and r["payWeek"] is None


async def test_two_pay_weeks_have_a_column_per_pay_week(db, monkeypatch):
    at(monkeypatch, 2026, 11, 18, 9)
    pedro, ana = await setup()
    await bid(ana, 2026, 11, 10, 10, n=2)             # first pay week
    await bid(ana, 2026, 11, 16, 10, n=4)             # second
    r = await perf(pedro)
    assert [(w["start"], w["end"], w["partial"]) for w in r["weeks"]] == [
        ("2026-11-08", "2026-11-14", False), ("2026-11-15", "2026-11-18", True)]
    row = r["bidders"][0]
    assert row["weekly"] == [2, 4] and row["total"] == 6


async def test_a_past_anchor_gives_two_whole_pay_weeks(db, monkeypatch):
    at(monkeypatch, 2026, 11, 18, 9)
    pedro, _ana = await setup()
    r = await perf(pedro, date="2026-11-10")          # the pay week of Nov 8..14, and the one before
    assert (r["start"], r["end"]) == ("2026-11-01", "2026-11-14")
    assert [(w["start"], w["end"], w["partial"]) for w in r["weeks"]] == [
        ("2026-11-01", "2026-11-07", False), ("2026-11-08", "2026-11-14", False)]


async def test_previous_period_is_the_two_pay_weeks_before(db, monkeypatch):
    at(monkeypatch, 2026, 11, 18, 9)
    pedro, ana = await setup()
    await bid(ana, 2026, 10, 27, 10, n=3)             # Tue of the earlier pay week (Oct 25 ..)
    await bid(ana, 2026, 11, 3, 10, n=1)              # Tue of the pay week before the current two
    await bid(ana, 2026, 11, 10, 10, n=2)
    await bid(ana, 2026, 11, 16, 10, n=4)
    row = (await perf(pedro))["bidders"][0]
    assert (row["total"], row["previousTotal"]) == (6, 4)


async def test_one_pay_week_is_unchanged_by_the_default(db, monkeypatch):
    at(monkeypatch, 2026, 11, 18, 9)
    pedro, _ana = await setup()
    r = await perf(pedro, weeks=1)
    assert (r["start"], r["end"]) == ("2026-11-15", "2026-11-18")
    assert len(r["weeks"]) == 7 and r["payWeek"]["label"] == "Week ending Sat Nov 21"


async def test_insights_follow_two_pay_weeks(db, monkeypatch):
    at(monkeypatch, 2026, 11, 18, 9)
    pedro, _ana = await setup()
    r = await bids_router.bids_insights(pedro, period="week", date="", to="", weeks=2, ownerId=None)
    assert (r["start"], r["end"], r["label"]) == ("2026-11-08", "2026-11-18", "Nov 8–21, 2026")


async def test_weeks_must_be_one_or_two(db, monkeypatch):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.deps import get_current_user

    at(monkeypatch, 2026, 11, 18, 9)
    pedro, _ana = await setup()
    app = FastAPI()
    app.include_router(bids_router.router)
    app.dependency_overrides[get_current_user] = lambda: pedro
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        codes = {
            (path, w): (await c.get(path, params={"period": "week", "weeks": w})).status_code
            for path in ("/bids/performance", "/bids/insights") for w in (1, 2, 3, 0)
        }
    assert codes == {(p, w): 200 if w in (1, 2) else 422 for p in ("/bids/performance", "/bids/insights") for w in (1, 2, 3, 0)}
