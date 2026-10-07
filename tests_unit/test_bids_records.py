"""Bid records store against an in-memory MongoDB (mongomock-motor); no real database."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from beanie import PydanticObjectId, init_beanie
from mongomock_motor import AsyncMongoMockClient
from pydantic import ValidationError

from app.bids import records
from app.bids.records import (
    confirm_upload, count_by_status, job_key, record_screenshot, refresh_report_rows, uses_records,
)
from app.bids.windows import BidSchedule
from app.config import Settings, get_settings
from app.models.bid import Bid
from app.models.bid_report import BidReport
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
    await init_beanie(database=database, document_models=[Bidder, Bid, BidReport])
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


CT = ZoneInfo("America/Chicago")
SCHED = BidSchedule(tz=CT, daily_at=time(18, 30), weekly_day=5, weekly_at=time(16, 0))


def settings_with(start):
    return get_settings().model_copy(update={"BID_RECORDS_START": start})


async def add_bid(bidder: Bidder, name: str, at: datetime, status: str = "pending") -> Bid:
    naive = at.astimezone(timezone.utc).replace(tzinfo=None)
    return await Bid(
        bidderId=bidder.id, ownerId=bidder.createdBy, jobKey=f"{bidder.folder}/2026-10-05/backend/{name}",
        firstAt=naive, status=status,
    ).insert()


async def test_count_by_status_counts_each_status_in_the_window(db):
    ana, raj = await make_bidder("Ana", "ana"), await make_bidder("Raj", "raj")
    start, end = T0, T0 + timedelta(days=1)
    await add_bid(ana, "a1", start, "approved")                              # [start is included
    await add_bid(ana, "a2", start + timedelta(hours=1), "approved")
    await add_bid(ana, "a3", start + timedelta(hours=2), "pending")
    await add_bid(ana, "a4", start + timedelta(hours=3), "rejected")
    await add_bid(ana, "a5", end, "approved")                                # end) is excluded
    await add_bid(ana, "a6", start - timedelta(seconds=1), "approved")       # before the window
    await add_bid(raj, "r1", start + timedelta(hours=4), "rejected")

    nobody = PydanticObjectId()
    got = await count_by_status([ana.id, raj.id, nobody], start, end)
    assert got == {str(ana.id): (2, 1, 1), str(raj.id): (0, 0, 1), str(nobody): (0, 0, 0)}


async def test_count_by_status_with_no_bidders_does_no_query(db):
    assert await count_by_status([], T0, T0 + timedelta(days=1)) == {}


def test_uses_records_everywhere_when_start_is_unset(monkeypatch):
    midnight = datetime(2026, 10, 6, 5, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(records, "get_settings", lambda: settings_with(None))
    assert records.records_start_utc(SCHED) is None
    assert uses_records(midnight, SCHED) is True
    assert uses_records(midnight - timedelta(days=400), SCHED) is True  # the database is the source by default


def test_uses_records_only_for_windows_starting_on_or_after_the_start_day(monkeypatch):
    midnight = datetime(2026, 10, 6, 5, 0, tzinfo=timezone.utc)  # Chicago midnight of 2026-10-06 is 05:00Z (CDT)

    monkeypatch.setattr(records, "get_settings", lambda: settings_with("2026-10-06"))
    assert records.records_start_utc(SCHED) == midnight
    assert uses_records(midnight, SCHED) is True  # a window starting exactly at the start-day midnight
    assert uses_records(midnight + timedelta(hours=18, minutes=30), SCHED) is True
    assert uses_records(midnight - timedelta(minutes=1), SCHED) is False  # a window straddling it keeps folders
    assert uses_records(midnight - timedelta(days=7), SCHED) is False
    assert uses_records(midnight - timedelta(days=7), SCHED, settings_with(None)) is True  # an explicit Settings wins


@pytest.mark.parametrize("value, want", [(None, None), ("", None), (" 2026-10-06 ", "2026-10-06")])
def test_records_start_setting_accepts_a_date_or_nothing(value, want):
    assert Settings(BID_RECORDS_START=value).BID_RECORDS_START == want


@pytest.mark.parametrize("value", ["10/06/2026", "2026-13-01", "2026-10-6", "20261006"])
def test_records_start_setting_rejects_anything_else(value):
    with pytest.raises(ValidationError):
        Settings(BID_RECORDS_START=value)


async def test_refresh_report_rows_updates_the_stored_day(db):
    ana, raj = await make_bidder("Ana", "ana"), await make_bidder("Raj", "raj")
    start, end = datetime(2026, 10, 4, 23, 30, tzinfo=timezone.utc), datetime(2026, 10, 5, 23, 30, tzinfo=timezone.utc)

    def report(b, kind, s, e, count, pending):
        return BidReport(
            kind=kind, periodKey=f"{e:%Y-%m-%d}", periodStart=s, periodEnd=e, bidderId=b.id, ownerId=b.createdBy,
            bidderName=b.name, rate=0.5, count=count, amount=round(count * 0.5, 2), pending=pending, rejected=None if pending is None else 0,
        ).insert()

    day = await report(ana, "daily", start, end, 1, 1)
    week = await report(ana, "weekly", end - timedelta(days=7), end, 1, 1)
    other_day = await report(ana, "daily", start - timedelta(days=1), start, 0, 0)  # does not contain the bid
    raj_day = await report(raj, "daily", start, end, 0, 0)
    folder_day = await report(ana, "daily", end, end + timedelta(days=1), 3, None)  # a folder-counted row is left alone

    first = await add_bid(ana, "a1", start + timedelta(hours=2), "approved")
    pending = await add_bid(ana, "a2", start + timedelta(hours=3), "pending")
    await refresh_report_rows(first)

    for stored, want in ((day, (1, 1, 0, 0.5)), (week, (1, 1, 0, 0.5)), (other_day, (0, 0, 0, 0.0)),
                         (raj_day, (0, 0, 0, 0.0)), (folder_day, (3, None, None, 1.5))):
        row = await BidReport.get(stored.id)
        assert (row.count, row.pending, row.rejected, row.amount) == want

    await Bid.find_one(Bid.id == pending.id).update({"$set": {"status": "approved"}})
    await refresh_report_rows(pending)
    row = await BidReport.get(day.id)
    assert (row.count, row.pending, row.rejected, row.amount) == (2, 0, 0, 1.0)
