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
from fastapi import HTTPException
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


# ---- /bids/pending ---------------------------------------------------------------------------------------------


async def pending(user, **kw):
    return await bids_router.pending_bids(user, **{
        "period": "all", "date": "", "to": "", "weeks": 1, "ownerId": None, "bidderId": None, "since": None, **kw})


async def seed_pending(monkeypatch) -> dict:
    """Wed 2026-11-25 09:00 CST. Ana: pending Oct 1, Nov 17, Nov 24; two approved Nov 24 (one decided 14:59:30Z, one
    14:00Z the same day); Mia: pending Nov 24."""
    s = await seed()
    at(monkeypatch, datetime(2026, 11, 25, 9))
    ana, mia = s["ana"], s["mia"]
    for d in (datetime(2026, 10, 1, 10), datetime(2026, 11, 17, 10), datetime(2026, 11, 24, 10)):
        await bid(ana, "pending", d)
    z = lambda h, m, sec=0: datetime(2026, 11, 25, h, m, sec, tzinfo=timezone.utc).astimezone(CHI).replace(tzinfo=None)
    await bid(ana, "approved", datetime(2026, 11, 24, 11), reviewed_at=z(14, 59, 30))
    await bid(ana, "approved", datetime(2026, 11, 24, 12), reviewed_at=z(14, 0))
    await bid(mia, "pending", datetime(2026, 11, 24, 10))
    return s


async def test_pending_lists_every_pending_bid_of_any_age(db, switch_on, monkeypatch):
    s = await seed_pending(monkeypatch)
    r = await pending(s["pedro"], period="all")
    assert [b["firstAt"][:10] for b in r["bids"]] == ["2026-10-01", "2026-11-17", "2026-11-24"]
    assert {b["status"] for b in r["bids"]} == {"pending"}
    assert r["label"] == "All pending" and r["bidderName"] is None and r["timezone"] == "America/Chicago"


async def test_pending_for_a_day_and_a_week(db, switch_on, monkeypatch):
    s = await seed_pending(monkeypatch)
    p = s["pedro"]
    r = await pending(p, period="day", date="2026-11-24")
    assert (len(r["bids"]), r["label"]) == (1, "Tue Nov 24")
    r = await pending(p, period="day")
    assert (len(r["bids"]), r["label"]) == (0, "Wed Nov 25")
    r = await pending(p, period="week")
    assert (len(r["bids"]), r["label"]) == (1, "Week ending Sun Nov 29")
    r = await pending(p, period="week", date="2026-11-22")
    assert (len(r["bids"]), r["label"], r["bids"][0]["firstAt"][:10]) == (1, "Week ending Sun Nov 22", "2026-11-17")
    r = await pending(p, period="week", date="2026-11-22", weeks=2)
    assert (len(r["bids"]), r["label"]) == (1, "Nov 8\u201322, 2026")
    r = await pending(p, period="month", date="2026-11-01")
    assert (len(r["bids"]), r["label"]) == (2, "Nov 2026")


async def test_pending_since_keeps_bids_decided_while_open(db, switch_on, monkeypatch):
    s = await seed_pending(monkeypatch)
    r = await pending(s["pedro"], period="week", since="2026-11-25T14:59:00Z")
    assert [b["status"] for b in r["bids"]] == ["pending", "approved"]  # the 14:00 decision is not listed
    r = await pending(s["pedro"], period="week", since="2026-11-25T14:59:00+00:00")
    assert len(r["bids"]) == 2


async def test_pending_one_bidder(db, switch_on, monkeypatch):
    s = await seed_pending(monkeypatch)
    r = await pending(s["pedro"], bidderId=str(s["ana"].id))
    assert r["bidderName"] == "Ana" and len(r["bids"]) == 3
    with pytest.raises(HTTPException) as e:
        await pending(s["pedro"], bidderId=str(s["mia"].id))
    assert (e.value.status_code, e.value.detail) == (404, "bidder not found")


async def test_pending_staff_see_only_their_own(db, switch_on, monkeypatch):
    s = await seed_pending(monkeypatch)
    r = await pending(s["pedro"])
    assert "Mia" not in {b["bidderName"] for b in r["bids"]}
    assert (await pending(s["pedro"], ownerId=str(s["maria"].id)))["bids"] == []


async def test_pending_admin_any_owner_or_all(db, switch_on, monkeypatch):
    s = await seed_pending(monkeypatch)
    r = await pending(s["boss"], ownerId=str(s["maria"].id))
    assert {b["bidderName"] for b in r["bids"]} == {"Mia"} and len(r["bids"]) == 1
    assert len((await pending(s["boss"]))["bids"]) == 4


async def test_pending_bad_dates_are_400(db, switch_on, monkeypatch):
    s = await seed_pending(monkeypatch)
    for kw in ({"period": "day", "date": "2026-02-31"}, {"period": "day", "date": "tomorrow"},
               {"period": "week", "date": "2026-02-31"}, {"since": "yesterday"}):
        with pytest.raises(HTTPException) as e:
            await pending(s["pedro"], **kw)
        assert e.value.status_code == 400, kw


async def test_pending_same_first_at_is_ordered_by_id(db, switch_on, monkeypatch):
    s = await seed_pending(monkeypatch)
    same = datetime(2026, 11, 24, 15)
    made = [await bid(s["raj"], "pending", same) for _ in range(3)]
    expected = sorted(str(b.id) for b in made)
    for _ in range(2):  # a refresh returns the same order
        r = await pending(s["pedro"], period="day", date="2026-11-24", bidderId=str(s["raj"].id))
        assert [b["id"] for b in r["bids"]] == expected


async def test_pending_includes_an_archived_bidders_bids(db, switch_on, monkeypatch):
    s = await seed_pending(monkeypatch)
    lee = await bid(s["lee"], "pending", datetime(2026, 11, 24, 9))
    r = await pending(s["pedro"], period="day", date="2026-11-24")
    assert str(lee.id) in {b["id"] for b in r["bids"]}
    assert str(lee.id) in {b["id"] for b in (await pending(s["pedro"]))["bids"]}
