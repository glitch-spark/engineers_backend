"""The Bids page's endpoints (plan 2026-11-16-bids-page-redesign): the server's report clock (/bids/clock), so the
client never hard-codes the pay-week schedule (mongomock-motor). Later tasks add the pending, backlog and pay routes'
tests here and reuse `db`, `at`, `seed` and `bid`."""
import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

import itertools
from datetime import datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

import pytest
from beanie import init_beanie
from mongomock_motor import AsyncMongoMockClient

from app.models import Account, UserExtra
from app.models.bid import Bid
from app.models.bid_report import BidReport
from app.models.bidder import Bidder
from app.models.daily_plan import DailyPlan
from app.models.interview import Interview
from app.models.interview_question import InterviewQuestion
from app.routers import bids as bids_router
from tests_unit.test_bids_single_page import switch_on  # noqa: F401  (the switched schedule, Ruling 11)

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

    database = AsyncMongoMockClient()["bids-page-endpoints-unit"]
    await init_beanie(database=database, document_models=[
        UserExtra, Account, Bidder, Bid, BidReport, DailyPlan, Interview, InterviewQuestion])
    settings = get_settings().model_copy(update={"BID_RECORDS_START": None})
    monkeypatch.setattr(records, "get_settings", lambda: settings)
    yield database


def at(monkeypatch, local: datetime) -> None:
    """The router's clock reads `local` (a naive America/Chicago time)."""
    moment = local.replace(tzinfo=CHI).astimezone(timezone.utc)
    monkeypatch.setattr(bids_router, "_now", lambda: moment)


async def seed() -> dict:
    """Two staff owners and an admin; Pedro has Ana, Raj and an archived Lee, Maria has Mia."""
    pedro = await UserExtra(name="Pedro", email="pedro@x.io", role="staff").insert()
    maria = await UserExtra(name="Maria", email="maria@x.io", role="staff").insert()
    boss = await UserExtra(name="Boss", email="boss@x.io", role="admin").insert()

    async def bidder(owner, name, rate, archived=False):
        return await Bidder(createdBy=owner.id, name=name, rate=rate, folder=name.lower(),
                            screenshotFolderUrl=f"s3://shots/{name.lower()}/", createdAt=datetime(2026, 1, 1),
                            registeredAt=datetime(2026, 1, 1),
                            archivedAt=datetime(2026, 10, 1) if archived else None).insert()

    return {
        "pedro": pedro, "maria": maria, "boss": boss,
        "ana": await bidder(pedro, "Ana", 0.06), "raj": await bidder(pedro, "Raj", 0.06),
        "lee": await bidder(pedro, "Lee", 0.06, archived=True), "mia": await bidder(maria, "Mia", 0.5),
    }


async def bid(bidder: Bidder, status: str, local_dt: datetime, reviewed_at: Optional[datetime] = None) -> Bid:
    """One bid of `bidder` first seen at `local_dt` (naive Chicago time); `reviewed_at` is naive Chicago too."""
    def utc(d):
        return d.replace(tzinfo=CHI).astimezone(timezone.utc).replace(tzinfo=None)
    return await Bid(bidderId=bidder.id, ownerId=bidder.createdBy, jobKey=f"{bidder.folder}/x/j{next(_n)}",
                     jobUrl="https://j.example/x", jobTitle="x", firstAt=utc(local_dt), status=status,
                     reviewedAt=utc(reviewed_at) if reviewed_at else None, screenshots=[]).insert()


# ---- /bids/clock -----------------------------------------------------------------------------------------------


async def test_clock_in_the_long_week(db, switch_on, monkeypatch):
    s = await seed()
    at(monkeypatch, datetime(2026, 11, 18, 9))  # Wed: the pay week Sun Nov 15 .. Sun Nov 22 (8 report days)
    assert await bids_router.bids_clock(s["pedro"]) == {
        "today": "2026-11-18", "payWeekStart": "2026-11-15", "payWeekEnd": "2026-11-22",
        "previousPayWeekStart": "2026-11-08", "timezone": "America/Chicago"}


async def test_clock_after_the_switch(db, switch_on, monkeypatch):
    s = await seed()
    at(monkeypatch, datetime(2026, 11, 22, 20))  # Sun 20:00: the long week closed
    r = await bids_router.bids_clock(s["boss"])
    assert (r["payWeekStart"], r["payWeekEnd"], r["today"]) == ("2026-11-23", "2026-11-29", "2026-11-23")
    assert r["previousPayWeekStart"] == "2026-11-15"  # the previous week is the 8-day one
    at(monkeypatch, datetime(2026, 11, 23, 18))  # Mon 18:00: Monday's report day closed
    assert (await bids_router.bids_clock(s["maria"]))["today"] == "2026-11-24"


async def test_clock_without_the_switch(db, monkeypatch):
    s = await seed()
    at(monkeypatch, datetime(2026, 11, 21, 15, 59))
    r = await bids_router.bids_clock(s["pedro"])
    assert (r["payWeekStart"], r["payWeekEnd"], r["today"]) == ("2026-11-15", "2026-11-21", "2026-11-21")
    assert r["previousPayWeekStart"] == "2026-11-08"
    at(monkeypatch, datetime(2026, 11, 21, 16))
    r = await bids_router.bids_clock(s["pedro"])
    assert (r["payWeekStart"], r["payWeekEnd"], r["previousPayWeekStart"]) == ("2026-11-22", "2026-11-28", "2026-11-15")
    at(monkeypatch, datetime(2026, 11, 21, 18, 30))
    assert (await bids_router.bids_clock(s["pedro"]))["today"] == "2026-11-22"
