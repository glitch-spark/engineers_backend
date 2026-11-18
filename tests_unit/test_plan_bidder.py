"""Bidder bids in the Daily Plan follow Bid Track (mongomock-motor)."""
import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

import itertools
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from beanie import init_beanie
from mongomock_motor import AsyncMongoMockClient

from app import plan_bidder
from app.models import Account, UserExtra
from app.models.bid import Bid
from app.models.bid_report import BidReport
from app.models.bidder import Bidder
from app.models.daily_plan import DailyPlan
from app.models.slack_alert_log import SlackAlertLog

pytestmark = [
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]

CHI = ZoneInfo("America/Chicago")
DAY = date(2026, 11, 18)
_n = itertools.count()


@pytest.fixture
async def db(monkeypatch):
    await init_beanie(database=AsyncMongoMockClient()["plan-bidder-unit"], document_models=[
        UserExtra, Account, Bidder, Bid, BidReport, DailyPlan, SlackAlertLog])
    monkeypatch.setattr(plan_bidder, "RELEASE_DAY", date(2026, 11, 1))


async def bidder_of(owner, name="Ana", archived=False):
    return await Bidder(createdBy=owner.id, name=name, rate=0.5, folder=name.lower(), screenshotFolderUrl=f"s3://s/{name.lower()}/",
                        archivedAt=datetime.utcnow() if archived else None).insert()


async def bid(bidder, hour, day=DAY, status="pending", kind="bidder"):
    first = datetime(day.year, day.month, day.day, hour, tzinfo=CHI).astimezone(timezone.utc).replace(tzinfo=None)
    return await Bid(kind=kind, bidderId=bidder.id, ownerId=bidder.createdBy, jobKey=f"{bidder.folder}/{next(_n)}",
                     jobUrl="https://j.io/1", firstAt=first, status=status).insert()


async def plan(owner, day=DAY):
    return await DailyPlan.get_motor_collection().find_one({"userId": owner.id, "date": datetime(day.year, day.month, day.day)})


async def test_counts_approved_and_pending_not_rejected(db):
    pedro = await UserExtra(name="P", email="p@x.io").insert()
    other = await UserExtra(name="O", email="o@x.io").insert()
    ana, archived, theirs = await bidder_of(pedro), await bidder_of(pedro, "Old", archived=True), await bidder_of(other, "Bo")
    for b, st in ((ana, "approved"), (ana, "pending"), (archived, "pending"), (ana, "rejected"), (theirs, "approved")):
        await bid(b, 10, status=st)
    await bid(ana, 10, kind="self")
    assert await plan_bidder.bidder_done(pedro.id, DAY) == 3


async def test_report_day_cutoff_respected(db):
    pedro = await UserExtra(name="P", email="p@x.io").insert()
    ana = await bidder_of(pedro)
    await bid(ana, 10)  # in DAY
    await bid(ana, 10, day=DAY - timedelta(days=1))  # the day before
    await bid(ana, 10, day=DAY + timedelta(days=1))  # the day after
    assert await plan_bidder.bidder_done(pedro.id, DAY) == 1


async def test_creates_plan_without_logged_at(db):
    pedro = await UserExtra(name="P", email="p@x.io").insert()
    await bid(await bidder_of(pedro), 10)
    await plan_bidder.write_through_bidder(pedro.id, DAY)
    await plan_bidder.write_through_bidder(pedro.id, DAY)  # replay: same effect
    p = await plan(pedro)
    assert p["done"]["bidsBidder"] == 1 and not p.get("loggedAt") and p["year"] == 2026
    assert await DailyPlan.get_motor_collection().count_documents({}) == 1


async def test_before_release_day_untouched(db, monkeypatch):
    monkeypatch.setattr(plan_bidder, "RELEASE_DAY", date(2026, 11, 19))
    pedro = await UserExtra(name="P", email="p@x.io").insert()
    await bid(await bidder_of(pedro), 10)
    await plan_bidder.write_through_bidder(pedro.id, DAY)
    assert await plan(pedro) is None


async def test_keeps_results_focus_notes(db):
    pedro = await UserExtra(name="P", email="p@x.io").insert()
    await bid(await bidder_of(pedro), 10)
    await DailyPlan.get_motor_collection().insert_one(
        {"userId": pedro.id, "date": datetime(2026, 11, 18), "results": [{"text": "r"}], "focus": "f", "notes": "n",
         "done": {"bidsBidder": 9, "bidsSelf": 4}})
    await plan_bidder.write_through_bidder(pedro.id, DAY)
    p = await plan(pedro)
    assert (p["results"], p["focus"], p["notes"]) == ([{"text": "r"}], "f", "n")
    assert p["done"] == {"bidsBidder": 1, "bidsSelf": 4}


async def test_rerenders_only_submitted_posted_plans(db, monkeypatch):
    from app import self_bids
    from app.routers import daily_plans

    pedro = await UserExtra(name="P", email="p@x.io").insert()
    await bid(await bidder_of(pedro), 10)
    synced = []

    async def fake_sync(plan, owner, done, rounds):
        synced.append(plan.slackTs)

    async def fake_interviews(*a):
        return {}

    monkeypatch.setattr(daily_plans, "_sync_slack", fake_sync)
    monkeypatch.setattr(daily_plans, "_interviews", fake_interviews)
    coll = DailyPlan.get_motor_collection()
    key = {"userId": pedro.id, "date": datetime(2026, 11, 18)}
    await coll.insert_one({**key, "year": 2026, "weekNumber": 47, "slackTs": "1.1"})
    await plan_bidder.write_through_bidder(pedro.id, DAY)  # posted but not submitted
    assert synced == []
    await coll.update_one(key, {"$set": {"submittedAt": datetime(2026, 11, 18, 23)}})
    await plan_bidder.write_through_bidder(pedro.id, DAY)
    assert synced == ["1.1"]


async def test_review_and_upload_call_it(db, monkeypatch):
    """Review: single and bulk (once per owner and day). Upload is in test_bidder_ext."""
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.deps import get_current_user
    from app.routers import bids

    pedro = await UserExtra(name="P", email="p@x.io", role="admin").insert()
    ana = await bidder_of(pedro)
    a, b, c = await bid(ana, 10), await bid(ana, 11), await bid(ana, 10, day=DAY + timedelta(days=1))
    calls = []

    async def spy(owner_id, day):
        calls.append((owner_id, day))

    monkeypatch.setattr(plan_bidder, "write_through_bidder", spy)
    app = FastAPI()
    app.include_router(bids.router)
    app.dependency_overrides[get_current_user] = lambda: pedro
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as cl:
        r = await cl.put(f"/bids/{a.id}/review", json={"status": "approved"})
        assert r.status_code == 200, r.text
        assert calls == [(pedro.id, DAY)]
        calls.clear()
        r = await cl.post("/bids/review", json={"ids": [str(x.id) for x in (a, b, c)], "status": "approved"})
        assert r.status_code == 200, r.text
    assert sorted(calls) == [(pedro.id, DAY), (pedro.id, DAY + timedelta(days=1))]


async def test_created_plan_is_not_counted_as_logged(db):
    """A bidder upload's plan stores loggedAt: None, so reports / dashboard don't read it as a legacy logged day."""
    from app.routers import dashboard, reports

    pedro = await UserExtra(name="P", email="p@x.io").insert()
    await bid(await bidder_of(pedro), 10)
    await plan_bidder.write_through_bidder(pedro.id, DAY)
    p = await plan(pedro)
    assert "loggedAt" in p and p["loggedAt"] is None
    assert await reports._last_logged([pedro.id], datetime(2026, 11, 30)) == {}
    assert await DailyPlan.get_motor_collection().count_documents({"userId": pedro.id, **dashboard._LOGGED_OR_LEGACY}) == 0
