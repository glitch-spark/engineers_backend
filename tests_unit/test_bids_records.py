"""Bid records store against an in-memory MongoDB (mongomock-motor); no real database."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from datetime import datetime, timedelta, timezone

import pytest
from beanie import PydanticObjectId, init_beanie
from mongomock_motor import AsyncMongoMockClient

from app.bids.records import confirm_upload, job_key, record_screenshot
from app.models.bid import Bid
from app.models.bidder import Bidder

pytestmark = [  # pre-existing utcnow defaults (app/models/base.py) and Beanie internals, not bids code
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]

JOB = "ana/2026-10-05/backend/acme-engineer"
T0 = datetime(2026, 10, 5, 14, 0, 0, tzinfo=timezone.utc)


def key(step: int, trigger: str, at: datetime) -> str:
    return f"{JOB}/step{step}_{trigger}_{at:%Y-%m-%d}_{at:%H%M%S}.png"


@pytest.fixture
async def db():
    database = AsyncMongoMockClient()["bid-records-unit"]  # fresh in-memory database per test
    await init_beanie(database=database, document_models=[Bidder, Bid])
    return database


async def make_bidder(name="Ana", folder="ana") -> Bidder:
    return await Bidder(
        createdBy=PydanticObjectId(), name=name, rate=0.5, screenshotFolderUrl=f"s3://shots/{folder}/", folder=folder,
    ).insert()


def test_job_key_is_the_key_up_to_its_last_slash():
    assert job_key(key(1, "click", T0)) == JOB


async def test_screenshots_of_one_job_make_one_bid(db):
    bidder = await make_bidder()
    # recorded out of order: firstAt is the earliest capture, not the first request
    shots = [(2, "next", T0 + timedelta(seconds=40)), (1, "click", T0), (3, "submit", T0 + timedelta(seconds=90))]
    for step, trigger, at in shots:
        await record_screenshot(bidder, key(step, trigger, at), step, trigger, at, None, None)

    bids = await Bid.find_all().to_list()
    assert len(bids) == 1
    bid = bids[0]
    assert len(bid.screenshots) == 3
    assert bid.bidderId == bidder.id
    assert bid.ownerId == bidder.createdBy
    assert bid.jobKey == JOB
    assert bid.firstAt == T0.replace(tzinfo=None)
    assert bid.submittedAt == (T0 + timedelta(seconds=90)).replace(tzinfo=None)
    assert bid.status == "pending"
    assert bid.changedSinceReview is False
    assert all(s.uploadedAt is None for s in bid.screenshots)


async def test_a_bid_without_submit_has_no_submitted_at(db):
    bidder = await make_bidder()
    bid = await record_screenshot(bidder, key(1, "click", T0), 1, "click", T0, None, None)
    assert bid.submittedAt is None


async def test_non_utc_capture_time_is_stored_as_naive_utc(db):
    bidder = await make_bidder()
    local = T0.astimezone(timezone(timedelta(hours=-5)))
    bid = await record_screenshot(bidder, key(1, "click", local), 1, "click", local, None, None)
    assert bid.firstAt == T0.replace(tzinfo=None)
    assert bid.screenshots[0].capturedAt == T0.replace(tzinfo=None)


async def test_the_same_job_of_another_bidder_is_another_bid(db):
    ana, ben = await make_bidder(), await make_bidder("Ben", "ben")
    await record_screenshot(ana, key(1, "click", T0), 1, "click", T0, None, None)
    await record_screenshot(ben, key(1, "click", T0), 1, "click", T0, None, None)
    assert await Bid.find_all().count() == 2


async def test_a_retried_request_does_not_duplicate_a_screenshot(db):
    bidder = await make_bidder()
    k = key(1, "click", T0)
    await record_screenshot(bidder, k, 1, "click", T0, None, None)
    bid = await record_screenshot(bidder, k, 1, "click", T0, None, None)
    assert await Bid.find_all().count() == 1
    assert [s.key for s in bid.screenshots] == [k]


async def test_first_job_url_and_title_are_kept(db):
    bidder = await make_bidder()
    await record_screenshot(bidder, key(1, "click", T0), 1, "click", T0, None, None)
    t1 = T0 + timedelta(seconds=10)
    await record_screenshot(bidder, key(2, "next", t1), 2, "next", t1, "https://a.io/j/1", "Engineer")
    t2 = T0 + timedelta(seconds=20)
    bid = await record_screenshot(bidder, key(3, "next", t2), 3, "next", t2, "https://b.io/j/2", "Other")
    assert bid.jobUrl == "https://a.io/j/1"
    assert bid.jobTitle == "Engineer"
    assert len(bid.screenshots) == 3


async def test_a_screenshot_after_review_flags_the_bid(db):
    bidder = await make_bidder()
    bid = await record_screenshot(bidder, key(1, "click", T0), 1, "click", T0, None, None)
    await Bid.find_one(Bid.id == bid.id).update({"$set": {"status": "approved"}})

    # a retry of a screenshot already listed is not new, so it does not flag the bid
    same = await record_screenshot(bidder, key(1, "click", T0), 1, "click", T0, None, None)
    assert same.changedSinceReview is False

    t1 = T0 + timedelta(seconds=30)
    after = await record_screenshot(bidder, key(2, "submit", t1), 2, "submit", t1, None, None)
    assert after.status == "approved"
    assert after.changedSinceReview is True
    assert len(after.screenshots) == 2


async def test_a_screenshot_of_a_pending_bid_does_not_flag_it(db):
    bidder = await make_bidder()
    await record_screenshot(bidder, key(1, "click", T0), 1, "click", T0, None, None)
    t1 = T0 + timedelta(seconds=30)
    bid = await record_screenshot(bidder, key(2, "next", t1), 2, "next", t1, None, None)
    assert bid.changedSinceReview is False


async def test_confirm_marks_only_this_bidders_key(db):
    ana, ben = await make_bidder(), await make_bidder("Ben", "ben")
    k = key(1, "click", T0)
    await record_screenshot(ana, k, 1, "click", T0, None, None)

    assert await confirm_upload(ben.id, k) is False
    assert (await Bid.find_one()).screenshots[0].uploadedAt is None

    assert await confirm_upload(ana.id, k) is True
    assert (await Bid.find_one()).screenshots[0].uploadedAt is not None
    assert await confirm_upload(ana.id, f"{JOB}/unknown.png") is False


async def test_confirm_marks_only_the_matching_screenshot(db):
    bidder = await make_bidder()
    t1 = T0 + timedelta(seconds=10)
    k1, k2 = key(1, "click", T0), key(2, "next", t1)
    await record_screenshot(bidder, k1, 1, "click", T0, None, None)
    await record_screenshot(bidder, k2, 2, "next", t1, None, None)
    assert await confirm_upload(bidder.id, k2) is True
    by_key = {s.key: s for s in (await Bid.find_one()).screenshots}
    assert by_key[k1].uploadedAt is None
    assert by_key[k2].uploadedAt is not None


async def test_bid_indexes(db):
    info = await db["bids"].index_information()
    keys = {name: (list(ix["key"]), bool(ix.get("unique"))) for name, ix in info.items() if name != "_id_"}
    assert sorted(keys.values()) == sorted([
        ([("bidderId", 1), ("jobKey", 1)], True),
        ([("ownerId", 1), ("firstAt", 1)], False),
        ([("bidderId", 1), ("firstAt", 1)], False),
        ([("status", 1), ("firstAt", 1)], False),
    ])


async def test_a_parallel_first_screenshot_is_retried_after_a_duplicate_key(db, monkeypatch):
    """Real MongoDB can reject one of two simultaneous upserts of a new job with a duplicate key."""
    from pymongo.errors import DuplicateKeyError

    bidder = await make_bidder()
    coll = Bid.get_motor_collection()
    real, calls = coll.find_one_and_update, []

    async def flaky(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise DuplicateKeyError("E11000 duplicate key")
        return await real(*args, **kwargs)

    monkeypatch.setattr(type(coll), "find_one_and_update", lambda self, *a, **k: flaky(*a, **k))
    bid = await record_screenshot(bidder, key(1, "click", T0), 1, "click", T0, None, None)
    assert len(calls) == 2
    assert len(bid.screenshots) == 1
