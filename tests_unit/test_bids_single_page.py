"""One Bidders page (spec 2026-11-14-bidders-single-page-design.md): in week mode the performance response carries the
pay week board (each row's day cells and pay-week block, the board totals and today), so money and "to review" match
/bids/week; every row says the bidder's status; the card's tops add streak and interviews (mongomock-motor)."""
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

    database = AsyncMongoMockClient()["bids-single-page-unit"]
    await init_beanie(database=database, document_models=[
        UserExtra, Account, Bidder, Bid, DailyPlan, Interview, InterviewQuestion])
    settings = get_settings().model_copy(update={"BID_RECORDS_START": None})
    monkeypatch.setattr(records, "get_settings", lambda: settings)
    now = datetime(2026, 11, 18, 9, tzinfo=CHI).astimezone(timezone.utc)    # Wed morning
    monkeypatch.setattr(bids_router, "_now", lambda: now)
    ins.platform_cache.clear()
    yield database
    ins.platform_cache.clear()


async def setup():
    pedro = await UserExtra(name="Pedro", email="pedro@x.io", role="staff").insert()
    ana = await Bidder(createdBy=pedro.id, name="Ana", rate=0.5, folder="ana", screenshotFolderUrl="s3://shots/ana/",
                       createdAt=datetime(2026, 1, 1), registeredAt=datetime(2026, 1, 1), country="BR").insert()
    bo = await Bidder(createdBy=pedro.id, name="Bo", rate=0.25, folder="bo", screenshotFolderUrl="s3://shots/bo/",
                      createdAt=datetime(2026, 1, 1)).insert()          # invited, never registered
    for status, n, day in (("approved", 10, 16), ("pending", 4, 17), ("rejected", 2, 17)):
        first = datetime(2026, 11, day, 10, tzinfo=CHI).astimezone(timezone.utc).replace(tzinfo=None)
        for _ in range(n):
            await Bid(bidderId=ana.id, ownerId=pedro.id, jobKey=f"ana/x/j{next(_n)}", jobUrl="https://j.example/x",
                      jobTitle="x", firstAt=first, status=status, screenshots=[]).insert()
    return pedro, ana, bo


async def perf(u, **kw):
    return await bids_router.bids_performance(
        u, **{"period": "week", "date": "", "to": "", "includeArchived": False, "ownerId": None, **kw})


async def board(u, **kw):
    return await bids_router.bids_week(u, **{"week": None, "includeArchived": False, "ownerId": None, **kw})


async def test_week_rows_carry_the_board_cells_and_pay(db):
    pedro, ana, _bo = await setup()
    r, b = await perf(pedro), await board(pedro)
    rows, cells = {x["id"]: x for x in r["bidders"]}, {x["id"]: x for x in b["bidders"]}
    assert rows[str(ana.id)]["dayCells"] == cells[str(ana.id)]["days"]
    assert rows[str(ana.id)]["week"] == cells[str(ana.id)]["week"]
    assert rows[str(ana.id)]["week"]["pay"] == 5.0 and rows[str(ana.id)]["week"]["toReview"] == 4


async def test_week_totals_match_the_board_and_say_today(db):
    pedro, _ana, _bo = await setup()
    r, b = await perf(pedro), await board(pedro)
    t = r["totals"]
    assert (t["toReview"], t["pay"], t["days"]) == (b["totals"]["toReview"], b["totals"]["pay"], b["totals"]["days"])
    assert (t["approved"], t["pending"], t["rejected"]) == (b["totals"]["approved"], b["totals"]["pending"], b["totals"]["rejected"])
    assert t["today"] == {"day": "2026-11-18", "approved": 0, "pending": 0}
    past = await perf(pedro, date="2026-11-04")
    assert past["totals"]["today"] is None


async def test_every_row_says_the_bidder_status(db):
    pedro, ana, bo = await setup()
    for period in ("week", "month"):
        rows = {x["id"]: x for x in (await perf(pedro, period=period))["bidders"]}
        assert (rows[str(ana.id)]["bidderStatus"], rows[str(bo.id)]["bidderStatus"]) == ("active", "invited")
        assert rows[str(ana.id)]["country"] == "BR" and rows[str(ana.id)]["toReview"] == 4
    month = (await perf(pedro, period="month"))["bidders"][0]
    assert month["dayCells"] is None and month["week"] is None   # the board is a pay-week thing


async def test_card_tops_add_streak_and_interviews(db):
    pedro, _ana, _bo = await setup()
    tops = (await bids_router.bids_insights(pedro, period="week", date="", to="", ownerId=None))["tops"]
    assert {"streak", "interviews"} <= set(tops)
    assert all("id" not in (t["platform"] or {}) for t in tops.values())


async def test_the_owners_self_bids_get_their_own_row_outside_the_totals(db):
    pedro, _ana, _bo = await setup()
    alpha = await Account(name="Alpha", createdBy=pedro.id).insert()
    for day, n in ((16, 3), (17, 2)):
        first = datetime(2026, 11, day, 10, tzinfo=CHI).astimezone(timezone.utc).replace(tzinfo=None)
        for _ in range(n):
            await Bid(kind="self", bidderId=None, ownerId=pedro.id, accountId=alpha.id, jobKey=f"self-{pedro.id}/x/j{next(_n)}",
                      firstAt=first, status="approved", screenshots=[]).insert()
    r = await perf(pedro)
    assert r["selfRows"] == [{"ownerId": str(pedro.id), "ownerName": "Pedro", "total": 5,
                              "weekly": [0, 3, 2, 0, 0, 0, 0], "interviews": 0, "lastBidAt": r["selfRows"][0]["lastBidAt"]}]
    assert r["selfRows"][0]["lastBidAt"] is not None
    assert r["totals"]["total"] == 14 and r["totals"]["approved"] == 10   # Ana's bidder bids only


@pytest.fixture
def switch_on(monkeypatch):
    """The schedule switch (Sun 2026-11-22 20:00: daily 18:00, weekly Sunday 20:00) in the settings
    schedule_from_settings reads, and no folder listing."""
    from app.bids import windows
    from app.config import get_settings

    settings = get_settings().model_copy(update={
        "BID_SCHEDULE_SWITCH_AT": "2026-11-22T20:00", "BID_NEXT_DAILY_REPORT_TIME": "18:00",
        "BID_NEXT_WEEKLY_REPORT_DAY": "sunday", "BID_NEXT_WEEKLY_REPORT_TIME": "20:00"})
    monkeypatch.setattr(windows, "get_settings", lambda: settings)

    async def no_folders(bidders, *_a):
        return {str(b.id): None for b in bidders}
    monkeypatch.setattr(bids_router, "_folders_without_record", no_folders)


async def listing(u, **kw):
    return await bids_router.list_bids(u, **{"day": None, "bidderId": None, "status": None, "week": None,
                                             "self_": False, "ownerId": None, **kw})


async def test_listing_a_day_after_the_switch(db, switch_on, monkeypatch):
    pedro, ana, _bo = await setup()
    inside, outside = [], []
    for at, into in ((datetime(2026, 11, 22, 18, 40), inside), (datetime(2026, 11, 23, 17, 50), inside),
                     (datetime(2026, 11, 23, 18, 0), outside)):
        first = at.replace(tzinfo=CHI).astimezone(timezone.utc).replace(tzinfo=None)
        into.append(await Bid(bidderId=ana.id, ownerId=pedro.id, jobKey=f"ana/x/j{next(_n)}",
                              jobUrl="https://j.example/x", jobTitle="x", firstAt=first, status="pending",
                              screenshots=[]).insert())
    r = await listing(pedro, day="2026-11-23")
    assert sorted(b["id"] for b in r["bids"]) == sorted(str(b.id) for b in inside)
    assert (r["day"], r["start"], r["end"]) == ("2026-11-23", "2026-11-23T00:30:00Z", "2026-11-24T00:00:00Z")

    # The open window on Mon 11-23 is the same day: it ends at Monday's 18:00 cutoff.
    monkeypatch.setattr(bids_router, "_now", lambda: datetime(2026, 11, 23, 12, tzinfo=CHI).astimezone(timezone.utc))
    r = await listing(pedro)
    assert (r["day"], r["start"], r["end"]) == ("2026-11-23", "2026-11-23T00:30:00Z", "2026-11-24T00:00:00Z")
