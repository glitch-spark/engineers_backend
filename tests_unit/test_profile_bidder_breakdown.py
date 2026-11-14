"""Profiles list → Bids column: the bidder bids broken down by bidder (name and count), counted like the bidder total
(approved + pending, archived bidders too, on the Bid Track day) so the names add up to it (mongomock-motor)."""
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

from app.models import (
    Account, Bid, Bidder, DailyPlan, Interview, InterviewQuestion, JobApplicationLog, ResumeApplication, UserExtra,
)

pytestmark = [
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]

CHI = ZoneInfo("America/Chicago")
_job = itertools.count()


def chi(month, day, hh, mm=0) -> datetime:
    return datetime(2026, month, day, hh, mm, tzinfo=CHI)


@pytest.fixture
async def db():
    await init_beanie(database=AsyncMongoMockClient()["profile-bidder-breakdown-unit"], document_models=[
        UserExtra, Account, DailyPlan, Interview, InterviewQuestion, Bid, Bidder, JobApplicationLog, ResumeApplication])


@pytest.fixture
def today(monkeypatch):
    from app.routers import accounts

    monkeypatch.setattr(accounts, "_today", lambda: date(2026, 11, 13))


async def _bidder(owner, acc, name, archived=False) -> Bidder:
    return await Bidder(createdBy=owner.id, name=name, profileId=acc.id, rate=1, screenshotFolderUrl=f"s3://{name}/",
                        archivedAt=datetime(2026, 11, 10) if archived else None).insert()


async def _bids(bidder, when: datetime, status: str, n: int) -> None:
    first = when.astimezone(timezone.utc).replace(tzinfo=None)
    for _ in range(n):
        await Bid(bidderId=bidder.id, ownerId=bidder.createdBy, jobKey=f"k{next(_job)}", firstAt=first, status=status,
                  screenshots=[]).insert()


async def _setup():
    ana = await UserExtra(name="Ana", email="ana@x.io").insert()
    a = await Account(name="A", region="US", createdBy=ana.id, createdAt=datetime(2026, 1, 5, 15)).insert()
    bo, kim, zed = await _bidder(ana, a, "Bo"), await _bidder(ana, a, "Kim", archived=True), await _bidder(ana, a, "Zed")
    await _bids(bo, chi(11, 7, 10), "approved", 30)
    await _bids(bo, chi(11, 7, 11), "pending", 5)
    await _bids(bo, chi(11, 7, 12), "rejected", 3)       # rejected: never counted
    await _bids(kim, chi(11, 3, 10), "approved", 10)     # archived later: still counted
    await _bids(zed, chi(10, 20, 10), "approved", 4)     # before the range
    return ana, bo, kim, zed


async def _list(user, **kw):
    from app.routers.accounts import list_profile_metrics

    return await list_profile_metrics(user, **{"userId": "", **kw})


async def test_bids_by_bidder_in_a_date_range(db, today):
    ana, bo, kim, _zed = await _setup()
    row = (await _list(ana, from_="2026-11-02", to="2026-11-08"))["rows"][0]
    assert row["bidderBidsBy"] == [{"bidderId": str(bo.id), "name": "Bo", "bids": 35},
                                   {"bidderId": str(kim.id), "name": "Kim", "bids": 10}]
    assert sum(b["bids"] for b in row["bidderBidsBy"]) == row["bidderBids"]


async def test_bids_by_bidder_all_time(db, today):
    ana, bo, kim, zed = await _setup()
    row = (await _list(ana, weeks="all"))["rows"][0]
    assert [(b["name"], b["bids"]) for b in row["bidderBidsBy"]] == [("Bo", 35), ("Kim", 10), ("Zed", 4)]
    assert sum(b["bids"] for b in row["bidderBidsBy"]) == row["bidderBids"]


async def test_no_bidder_bids_is_an_empty_list(db, today):
    ana = await UserExtra(name="Ana", email="ana@x.io").insert()
    await Account(name="A", region="US", createdBy=ana.id, createdAt=datetime(2026, 1, 5, 15)).insert()
    row = (await _list(ana, from_="2026-11-02", to="2026-11-08"))["rows"][0]
    assert row["bidderBidsBy"] == []
