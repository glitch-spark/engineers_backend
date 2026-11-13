"""scripts/backfill_interview_applied_by: "Applied by" for interviews saved since Bid Track started (Oct 6) that have
none: a real, active bidder already bidding on the profile → that bidder (most bids), else self (mongomock)."""
import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")
os.environ["RESUME_QUEUE_DISABLED"] = "1"

from datetime import datetime

import pytest
from beanie import init_beanie
from mongomock_motor import AsyncMongoMockClient

from app.models import Account, Bid, Bidder, Interview, InterviewQuestion, UserExtra
from scripts import backfill_interview_applied_by as bf

pytestmark = [
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]

_n = iter(range(10**6))


@pytest.fixture
async def db():
    await init_beanie(database=AsyncMongoMockClient()["backfill-applied-unit"],
                      document_models=[UserExtra, Account, Bid, Bidder, Interview, InterviewQuestion])


async def _profile(owner, name):
    return await Account(name=name, createdBy=owner.id).insert()


async def _bidder(owner, acc, name, bids, first=datetime(2026, 10, 8, 15), archived=None):
    b = await Bidder(createdBy=owner.id, name=name, profileId=acc.id, rate=1, screenshotFolderUrl=f"s3://{name}/",
                     createdAt=datetime(2026, 10, 6), archivedAt=archived).insert()
    for i in range(bids):
        await Bid(bidderId=b.id, ownerId=owner.id, jobKey=f"k{next(_n)}", firstAt=first, status="pending",
                  screenshots=[]).insert()
    return b


async def _iv(owner, acc, saved, applied=None):
    return await Interview(accountId=acc.id, createdBy=owner.id, scheduledAt=saved, companyName="Acme",
                           createdAt=saved, appliedBy=applied).insert()


async def test_who_applied_is_decided_from_real_active_bidders(db):
    ana = await UserExtra(name="Ana", email="ana@x.io").insert()
    real, test, gone, two, solo = [await _profile(ana, n) for n in ("Real", "Test", "Gone", "Two", "Solo")]
    bo = await _bidder(ana, real, "Bo", 60)
    await _bidder(ana, test, "test-bidder", 5)                                   # under 50 bids: not a real bidder
    await _bidder(ana, gone, "Old", 80, archived=datetime(2026, 10, 13))        # archived before the interview
    await _bidder(ana, two, "Small", 60)
    big = await _bidder(ana, two, "Big", 120)
    on_real = await _iv(ana, real, datetime(2026, 10, 10, 15))
    early = await _iv(ana, real, datetime(2026, 10, 7, 15))                     # before Bo's first bid: self
    on_test = await _iv(ana, test, datetime(2026, 10, 10, 15))
    on_gone = await _iv(ana, gone, datetime(2026, 10, 20, 15))
    on_two = await _iv(ana, two, datetime(2026, 10, 10, 15))
    on_solo = await _iv(ana, solo, datetime(2026, 10, 10, 15))
    await _iv(ana, solo, datetime(2026, 10, 1, 15))                              # before Oct 6: not touched
    await _iv(ana, solo, datetime(2026, 10, 10, 15), applied={"kind": "bidder", "bidderName": "Kept"})  # set: kept

    planned = {iv_id: a for iv_id, a in await bf.plan()}
    assert set(planned) == {on_real.id, early.id, on_test.id, on_gone.id, on_two.id, on_solo.id}
    assert planned[on_real.id] == {"kind": "bidder", "bidderId": bo.id, "bidderName": "Bo", "backfilled": True}
    assert planned[on_two.id]["bidderId"] == big.id
    for iv in (early, on_test, on_gone, on_solo):
        assert planned[iv.id] == {"kind": "self", "backfilled": True}


async def test_apply_writes_only_interviews_still_without_applied_by(db):
    ana = await UserExtra(name="Ana", email="ana@x.io").insert()
    solo = await _profile(ana, "Solo")
    a, b = await _iv(ana, solo, datetime(2026, 10, 10, 15)), await _iv(ana, solo, datetime(2026, 10, 11, 15))
    planned = await bf.plan()
    await Interview.get_motor_collection().update_one(  # someone sets b's "Applied by" while the backfill runs
        {"_id": b.id}, {"$set": {"appliedBy": {"kind": "bidder", "bidderName": "Human"}}})
    assert await bf.apply(planned) == 1
    saved_a, saved_b = await Interview.get(a.id), await Interview.get(b.id)
    assert (saved_a.appliedBy.kind, saved_a.appliedBy.backfilled) == ("self", True)
    assert saved_b.appliedBy.bidderName == "Human" and saved_b.appliedBy.backfilled is False
