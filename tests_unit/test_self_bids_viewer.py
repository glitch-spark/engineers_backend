"""Seeing self bids (the owner's own applies from the extension): /bids?self=true lists them for a day or pay week
(own ones; an admin picks the owner), and a self bid's page and screenshots open for its owner and admins, read-only:
never reviewable (mongomock-motor)."""
import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

import itertools
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from beanie import init_beanie
from fastapi import HTTPException
from mongomock_motor import AsyncMongoMockClient

from app.models import Account, UserExtra
from app.models.bid import Bid
from app.models.bidder import Bidder
from app.models.daily_plan import DailyPlan
from app.models.interview import Interview
from app.models.interview_question import InterviewQuestion
from app.models.resume_application import ResumeApplication
from app.routers import bids as bids_router

pytestmark = [
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]

CHI = ZoneInfo("America/Chicago")
_n = itertools.count()


@pytest.fixture
async def db(monkeypatch):
    await init_beanie(database=AsyncMongoMockClient()["self-bids-viewer-unit"], document_models=[
        UserExtra, Account, Bidder, Bid, DailyPlan, Interview, InterviewQuestion, ResumeApplication])
    monkeypatch.setattr(bids_router, "_now", lambda: datetime(2026, 11, 18, 15, tzinfo=timezone.utc))

    async def no_thumbs(bids):
        return {str(b.id): None for b in bids}

    monkeypatch.setattr(bids_router, "_thumb_urls", no_thumbs)


async def setup():
    pedro = await UserExtra(name="Pedro", email="pedro@x.io").insert()
    maria = await UserExtra(name="Maria", email="maria@x.io").insert()
    root = await UserExtra(name="Root", email="root@x.io", role="admin").insert()
    alpha = await Account(name="Alpha", createdBy=pedro.id).insert()
    beta = await Account(name="Beta", createdBy=maria.id).insert()
    return pedro, maria, root, alpha, beta


async def self_bid(owner, account, day=17, hour=10):
    first = datetime(2026, 11, day, hour, tzinfo=CHI).astimezone(timezone.utc).replace(tzinfo=None)
    return await Bid(kind="self", bidderId=None, ownerId=owner.id, accountId=account.id, jobKey=f"self-{owner.id}/x/j{next(_n)}",
                     jobUrl="https://jobs.example/1", jobTitle="Backend", firstAt=first, status="approved",
                     screenshots=[]).insert()


async def listing(user, **kw):
    return await bids_router.list_bids(user, **{"day": None, "bidderId": None, "status": None, "week": None,
                                                "self_": True, "ownerId": None, **kw})


async def test_owners_list_their_own_self_bids_for_a_day(db):
    pedro, maria, _root, alpha, beta = await setup()
    mine = await self_bid(pedro, alpha)
    await self_bid(maria, beta)
    out = await listing(pedro, day="2026-11-17")
    assert [b["id"] for b in out["bids"]] == [str(mine.id)]
    b = out["bids"][0]
    assert (b["kind"], b["bidderName"], b["profileName"], b["status"]) == ("self", "Self", "Alpha", "approved")
    assert (await listing(pedro, ownerId=str(maria.id), day="2026-11-17"))["bids"] == []   # staff can't widen


async def test_a_pay_week_and_an_admin_picking_the_owner(db):
    pedro, maria, root, alpha, beta = await setup()
    await self_bid(pedro, alpha, day=16)
    await self_bid(pedro, alpha, day=17)
    await self_bid(maria, beta, day=17)
    assert len((await listing(pedro, week="2026-11-17"))["bids"]) == 2
    out = await listing(root, ownerId=str(maria.id), day="2026-11-17")
    assert [b["bidderName"] for b in out["bids"]] == ["Self · Maria"]


async def test_the_bid_page_opens_read_only_for_its_owner_and_admins(db):
    pedro, maria, root, alpha, _beta = await setup()
    bid = await self_bid(pedro, alpha)
    assert (await bids_router.get_bid(str(bid.id), pedro))["kind"] == "self"
    assert (await bids_router.get_bid(str(bid.id), root))["profileName"] == "Alpha"
    with pytest.raises(HTTPException) as e:
        await bids_router.get_bid(str(bid.id), maria)
    assert e.value.status_code == 404


async def test_bidder_lists_stay_without_self_bids(db):
    pedro, _maria, _root, alpha, _beta = await setup()
    await self_bid(pedro, alpha)
    out = await listing(pedro, day="2026-11-17", self_=False)
    assert out["bids"] == []
