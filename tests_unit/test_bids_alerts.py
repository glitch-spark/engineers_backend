"""Bidders page banners (spec 2026-11-14-bidders-insights-design.md): bidders that need action (red, first kind wins:
inactive, rejects, no_interviews, review_backlog) and archive suggestions (idle, expired unused invite); unregistered
bidders never flagged (mongomock-motor). Today: Wed 2026-11-18; 14 days = Nov 5..18; last 3 weekdays = Nov 13, 16, 17."""
import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

import itertools
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from beanie import init_beanie
from mongomock_motor import AsyncMongoMockClient

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

    database = AsyncMongoMockClient()["bids-alerts-unit"]
    await init_beanie(database=database, document_models=[
        UserExtra, Account, Bidder, Bid, DailyPlan, Interview, InterviewQuestion])
    monkeypatch.setattr(bids_router, "_now", lambda: datetime(2026, 11, 18, 15, 0, tzinfo=timezone.utc))
    settings = get_settings().model_copy(update={"BID_RECORDS_START": None})
    monkeypatch.setattr(records, "get_settings", lambda: settings)
    return database


async def user(name, role="staff"):
    return await UserExtra(name=name, email=f"{name.lower()}@x.io", role=role).insert()


async def bidder(owner, name, profile=None, created=datetime(2026, 10, 1), registered=True, invite_expires=None):
    return await Bidder(createdBy=owner.id, name=name, rate=0.5, folder=name.lower(),
                        screenshotFolderUrl=f"s3://shots/{name.lower()}/", profileId=profile.id if profile else None,
                        createdAt=created, registeredAt=created if registered else None,
                        inviteExpiresAt=invite_expires).insert()


async def bids(b, month, day, n, status="approved", reason=None):
    at = datetime(2026, month, day, 10, tzinfo=CHI).astimezone(timezone.utc).replace(tzinfo=None)
    for _ in range(n):
        await Bid(bidderId=b.id, ownerId=b.createdBy, jobKey=f"{b.folder}/x/j{next(_n)}", jobUrl="https://j.example/x",
                  jobTitle="x", firstAt=at, status=status, rejectReason=reason, screenshots=[]).insert()


async def alerts(u, ownerId=None):
    return await bids_router.bids_alerts(u, ownerId=ownerId)


def kinds(out):
    return {f["name"]: f["kind"] for f in out["flags"]}


async def test_inactive_after_three_quiet_weekdays(db):
    pedro = await user("Pedro")
    ana, bo = await bidder(pedro, "Ana"), await bidder(pedro, "Bo")
    await bids(ana, 11, 12, 30)                       # Thu; Fri 13, Mon 16, Tue 17 quiet
    await bids(bo, 11, 12, 30)
    await bids(bo, 11, 17, 30)
    out = await alerts(pedro)
    assert kinds(out) == {"Ana": "inactive"}
    assert out["flags"][0]["detail"] == {"lastBidDay": "2026-11-12"}


async def test_rejects_needs_20_reviewed_and_20_percent(db):
    pedro = await user("Pedro")
    ana, bo = await bidder(pedro, "Ana"), await bidder(pedro, "Bo")
    for b, rej in ((ana, 4), (bo, 3)):
        await bids(b, 11, 17, 20 - rej)
    await bids(ana, 11, 16, 2, "rejected", "blurry")
    await bids(ana, 11, 16, 1, "rejected", "dup")
    await bids(ana, 11, 16, 1, "rejected")
    await bids(bo, 11, 16, 3, "rejected", "blurry")
    out = await alerts(pedro)
    assert kinds(out) == {"Ana": "rejects"}
    assert out["flags"][0]["detail"] == {"rejected": 4, "reviewed": 20, "topReason": "blurry"}


async def test_no_interviews_needs_100_bids_and_a_profile(db):
    pedro = await user("Pedro")
    p, q = await Account(name="Erric", createdBy=pedro.id).insert(), await Account(name="Q", createdBy=pedro.id).insert()
    ana, bo, cy = await bidder(pedro, "Ana", p), await bidder(pedro, "Bo", q), await bidder(pedro, "Cy")
    await bids(ana, 11, 17, 100)
    await bids(bo, 11, 17, 99)
    await bids(cy, 11, 17, 150)                       # no profile: interviews unknown
    out = await alerts(pedro)
    assert kinds(out) == {"Ana": "no_interviews"}
    assert out["flags"][0]["detail"] == {"bids": 100, "profileId": str(p.id), "profileName": "Erric"}


async def test_review_backlog_any_age(db):
    pedro = await user("Pedro")
    ana, bo = await bidder(pedro, "Ana"), await bidder(pedro, "Bo")
    for b in (ana, bo):
        await bids(b, 11, 17, 10)
    await bids(ana, 10, 1, 1, "pending")              # weeks old: still counts
    await bids(ana, 11, 16, 1, "pending")             # = today - 2
    await bids(bo, 11, 17, 3, "pending")              # yesterday: not yet
    out = await alerts(pedro)
    assert kinds(out) == {"Ana": "review_backlog"}
    assert out["flags"][0]["detail"] == {"pending": 2, "oldestDay": "2026-10-01"}


async def test_first_kind_wins(db):
    pedro = await user("Pedro")
    ana = await bidder(pedro, "Ana")
    await bids(ana, 11, 10, 5, "pending")             # backlog, and quiet since Nov 10
    assert kinds(await alerts(pedro)) == {"Ana": "inactive"}


async def test_archive_idle_and_expired_invite(db):
    pedro = await user("Pedro")
    idle = await bidder(pedro, "Idle")
    await bids(idle, 10, 30, 5)                       # last bid before the 14 days
    await bidder(pedro, "Expired", registered=False, invite_expires=datetime(2026, 11, 3, 18))
    await bidder(pedro, "Never", registered=False)    # never invited: neither list
    await bidder(pedro, "New", created=datetime(2026, 11, 10))   # too new
    out = await alerts(pedro)
    assert {a["name"]: (a["reason"], a["since"]) for a in out["archive"]} == {
        "Expired": ("invite_expired", "2026-11-03"), "Idle": ("idle", "2026-10-30")}
    assert out["flags"] == []


async def test_admin_needs_an_owner(db):
    root, maria = await user("Root", "admin"), await user("Maria")
    bo = await bidder(maria, "Bo")
    await bids(bo, 11, 12, 30)                        # quiet since Thu: inactive
    assert (await alerts(root))["flags"] is None
    out = await alerts(root, ownerId=str(maria.id))
    assert kinds(out) == {"Bo": "inactive"}
