"""Bidders page insights (spec 2026-11-14-bidders-insights-design.md): one owner's active bidders pooled vs every
owner's, and each measure's best bidder vs the best on the platform (names only for other owners) (mongomock-motor)."""
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

    database = AsyncMongoMockClient()["bids-insights-unit"]
    await init_beanie(database=database, document_models=[
        UserExtra, Account, Bidder, Bid, DailyPlan, Interview, InterviewQuestion])
    monkeypatch.setattr(bids_router, "_now", lambda: datetime(2026, 11, 4, 15, 0, tzinfo=timezone.utc))
    settings = get_settings().model_copy(update={"BID_RECORDS_START": None})
    monkeypatch.setattr(records, "get_settings", lambda: settings)
    ins.platform_cache.clear()
    yield database
    ins.platform_cache.clear()


async def user(name, role="staff"):
    return await UserExtra(name=name, email=f"{name.lower()}@x.io", role=role).insert()


async def bidder(owner, name, profile=None, archived=False):
    return await Bidder(createdBy=owner.id, name=name, rate=0.5, folder=name.lower(),
                        screenshotFolderUrl=f"s3://shots/{name.lower()}/", profileId=profile.id if profile else None,
                        archivedAt=datetime(2026, 11, 3) if archived else None, createdAt=datetime(2026, 1, 1)).insert()


async def profile(owner, name):
    return await Account(name=name, createdBy=owner.id).insert()


async def bids(b, n, status="approved", day=2):
    at = datetime(2026, 11, day, 10, tzinfo=CHI).astimezone(timezone.utc).replace(tzinfo=None)
    for _ in range(n):
        await Bid(bidderId=b.id, ownerId=b.createdBy, jobKey=f"{b.folder}/x/j{next(_n)}", jobUrl="https://j.example/x",
                  jobTitle="x", firstAt=at, status=status, screenshots=[]).insert()


async def rounds(p, owner, n, day=3):
    at = datetime(2026, 11, day, 10, tzinfo=CHI)
    for _ in range(n):
        await Interview(accountId=p.id, createdBy=owner.id, scheduledAt=at, companyName="Acme",
                        stageHistory=[{"stage": "intro", "at": at, "scheduledAt": at, "status": "completed"}]).insert()


async def call(u, ownerId=None):
    return await bids_router.bids_insights(u, period="month", date="", to="", ownerId=ownerId)


async def test_mine_pools_the_owners_active_bidders(db):
    pedro = await user("Pedro")
    p = await profile(pedro, "P")
    ana = await bidder(pedro, "Ana", p)
    await bidder(pedro, "Ben")                       # no bids: not in the pool
    await bids(ana, 100)
    await rounds(p, pedro, 2)
    m = (await call(pedro))["mine"]
    assert (m["bidders"], m["bids"], m["interviews"], m["per100"], m["pay"], m["costPerInterview"]) == (
        1, 100, 2, 2.0, 50.0, 25.0)
    assert (m["approved"], m["reviewed"], m["approvalRate"]) == (100, 100, 1.0)


async def test_shared_profile_interviews_count_once(db):
    pedro = await user("Pedro")
    p = await profile(pedro, "P")
    ana, cy = await bidder(pedro, "Ana", p), await bidder(pedro, "Cy", p)
    await bids(ana, 60)
    await bids(cy, 60)
    await rounds(p, pedro, 1)
    assert (await call(pedro))["mine"]["interviews"] == 1


async def test_platform_covers_every_owner_and_names_the_best(db):
    pedro, maria = await user("Pedro"), await user("Maria")
    p, q = await profile(pedro, "P"), await profile(maria, "Q")
    ana, bo = await bidder(pedro, "Ana", p), await bidder(maria, "Bo", q)
    await bids(ana, 100)
    await bids(bo, 100)
    await rounds(p, pedro, 1)
    await rounds(q, maria, 3)
    out = await call(pedro)
    assert out["platform"]["bidders"] == 2 and out["platform"]["interviews"] == 4
    assert out["tops"]["per100"] == {"mine": {"id": str(ana.id), "name": "Ana", "value": 1.0},
                                     "platform": {"name": "Bo", "ownerName": "Maria", "value": 3.0}}
    assert all("id" not in (t["platform"] or {}) for t in out["tops"].values())


async def test_tops_thresholds_and_ties(db):
    pedro = await user("Pedro")
    pa, pc, pd = await profile(pedro, "PA"), await profile(pedro, "PC"), await profile(pedro, "PD")
    ana, cy, dee = await bidder(pedro, "Ana", pa), await bidder(pedro, "Cy", pc), await bidder(pedro, "Dee", pd)
    await bids(ana, 99)
    await rounds(pa, pedro, 5)                       # 99 bids: no per-100 rate
    await bids(cy, 100)
    await rounds(pc, pedro, 2)                       # per100 2.0; only 2 interviews: not in cost tops
    await bids(dee, 200)
    await rounds(pd, pedro, 4)                       # per100 2.0 with more bids; cost 100 / 4 = 25
    tops = (await call(pedro))["tops"]
    assert tops["per100"]["mine"]["name"] == "Dee"
    assert tops["costPerInterview"]["mine"] == {"id": str(ana.id), "name": "Ana", "value": 9.9}   # 49.5 / 5
    assert tops["bids"]["mine"] == {"id": str(dee.id), "name": "Dee", "value": 200}


async def test_admin_needs_an_owner(db):
    root, maria = await user("Root", "admin"), await user("Maria")
    bo = await bidder(maria, "Bo")
    await bids(bo, 10)
    out = await call(root)
    assert (out["mine"], out["platform"], out["tops"]) == (None, None, None)
    assert (await call(root, ownerId=str(maria.id)))["mine"]["bids"] == 10


async def test_archived_bidders_never_count(db):
    pedro = await user("Pedro")
    old = await bidder(pedro, "Old", archived=True)
    await bids(old, 50)
    ana = await bidder(pedro, "Ana")
    await bids(ana, 5)
    out = await call(pedro)
    assert out["mine"]["bids"] == 5 and out["platform"]["bids"] == 5
