"""Bidders page periods (spec 2026-11-14-bidders-single-page-design.md): a week is the pay week (Sat 16:00 -> Sat
16:00) with a column per report day; a month / range has a column per pay week, clipped; today is the open report day
(after the 18:30 cutoff, tomorrow's) (mongomock-motor)."""
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

    database = AsyncMongoMockClient()["bids-period-unit"]
    await init_beanie(database=database, document_models=[
        UserExtra, Account, Bidder, Bid, DailyPlan, Interview, InterviewQuestion])
    settings = get_settings().model_copy(update={"BID_RECORDS_START": None})
    monkeypatch.setattr(records, "get_settings", lambda: settings)
    ins.platform_cache.clear()
    yield database
    ins.platform_cache.clear()


def at(monkeypatch, *args):
    """Freeze now at a Chicago wall-clock time."""
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
        u, **{"period": "week", "date": "", "to": "", "includeArchived": False, "ownerId": None, **kw})


async def test_a_week_is_the_pay_week_with_a_column_per_report_day(db, monkeypatch):
    at(monkeypatch, 2026, 11, 18, 9)                  # Wed morning
    pedro, _ana = await setup()
    r = await perf(pedro)
    assert (r["start"], r["end"]) == ("2026-11-15", "2026-11-18")      # Sun .. today (the week runs to Sat)
    assert len(r["weeks"]) == 7
    assert r["weeks"][0] == {"start": "2026-11-15", "end": "2026-11-15", "partial": False}
    assert r["weeks"][-1]["start"] == "2026-11-21"
    assert r["payWeek"]["label"] == "Week ending Sat Nov 21"
    assert [d["day"] for d in r["payWeek"]["days"]][:2] == ["2026-11-15", "2026-11-16"]


async def test_saturday_after_four_opens_the_next_pay_week(db, monkeypatch):
    at(monkeypatch, 2026, 11, 21, 17)                 # Sat 17:00: the new pay week, no report day started yet
    pedro, ana = await setup()
    await bid(ana, 2026, 11, 20, 10, n=5)
    r = await perf(pedro)
    assert r["payWeek"]["days"][0]["day"] == "2026-11-22"
    assert r["bidders"][0]["total"] == 0 and len(r["weeks"]) == 7


async def test_evening_bids_count_for_tomorrow_everywhere(db, monkeypatch):
    at(monkeypatch, 2026, 11, 18, 19, 30)             # Wed 19:30: Thu's report day is open
    pedro, ana = await setup()
    await bid(ana, 2026, 11, 18, 19)
    r = await perf(pedro)
    row = r["bidders"][0]
    assert r["end"] == "2026-11-19"
    assert row["weekly"][4] == 1 and row["total"] == 1                  # Sun, Mon, Tue, Wed, *Thu*


async def test_a_month_has_a_column_per_pay_week_clipped(db, monkeypatch):
    at(monkeypatch, 2026, 11, 18, 9)
    pedro, _ana = await setup()
    r = await perf(pedro, period="month")
    assert [(w["start"], w["end"], w["partial"]) for w in r["weeks"]] == [
        ("2026-11-01", "2026-11-07", False), ("2026-11-08", "2026-11-14", False), ("2026-11-15", "2026-11-18", True)]
    r = await perf(pedro, period="month", date="2026-10-15")
    cols = [(w["start"], w["end"], w["partial"]) for w in r["weeks"]]
    assert cols[0] == ("2026-10-01", "2026-10-03", True) and cols[-1] == ("2026-10-25", "2026-10-31", False)
    assert "payWeek" not in r or r["payWeek"] is None


async def test_previous_period_of_a_week_is_the_previous_pay_week(db, monkeypatch):
    at(monkeypatch, 2026, 11, 18, 9)
    pedro, ana = await setup()
    await bid(ana, 2026, 11, 10, 10, n=3)             # Tue of the previous pay week
    await bid(ana, 2026, 11, 16, 10, n=6)
    row = (await perf(pedro))["bidders"][0]
    assert (row["total"], row["previousTotal"]) == (6, 3)


async def test_insights_use_the_same_pay_week(db, monkeypatch):
    at(monkeypatch, 2026, 11, 18, 9)
    pedro, _ana = await setup()
    r = await bids_router.bids_insights(pedro, period="week", date="", to="", ownerId=None)
    assert (r["start"], r["end"], r["label"]) == ("2026-11-15", "2026-11-18", "Week ending Sat Nov 21")
