import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from datetime import datetime, time, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from beanie import PydanticObjectId, init_beanie
from mongomock_motor import AsyncMongoMockClient

from app.bids import records
from app.bids.live_counts import TtlCache, live_counts
from app.bids.storage import BidStorageError
from app.bids.windows import BidSchedule
from app.config import get_settings
from app.models.bid import Bid
from app.models.bidder import Bidder

pytestmark = [  # pre-existing utcnow defaults (app/models/base.py) and Beanie internals, not bids code
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]

CT = ZoneInfo("America/Chicago")
SCHED = BidSchedule(tz=CT, daily_at=time(18, 30), weekly_day=5, weekly_at=time(16, 0))


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


NOW = utc(2026, 10, 3, 3, 0)  # Fri Oct 2 22:00 CDT


class Storage:
    """One `calls` entry per folder listing."""

    def __init__(self, result=4): self.result, self.calls = result, []

    async def count_windows(self, url, windows):
        self.calls.append((url, list(windows)))
        if isinstance(self.result, Exception):
            raise self.result
        return [self.result] * len(windows)


def records_start(monkeypatch, value):
    monkeypatch.setattr(records, "get_settings", lambda: get_settings().model_copy(update={"BID_RECORDS_START": value}))


@pytest.fixture(autouse=True)
def no_records_start(monkeypatch):
    """A local .env must not change which source these folder-path tests count."""
    records_start(monkeypatch, "2099-01-01")  # these exercise the folder path


def bidder(url="s3://b/ana/"):
    return SimpleNamespace(id=PydanticObjectId(), screenshotFolderUrl=url)


def cache():
    return TtlCache(timedelta(minutes=5))


async def test_live_counts_use_open_windows():
    b, storage = bidder(), Storage()
    got = await live_counts([b], NOW, SCHED, storage, cache())
    assert got == {str(b.id): {"today": 4, "week": 4, "error": None}}
    # One listing counts both windows: today since Fri 18:30 CDT, the week since Sat 16:00 CDT.
    assert storage.calls == [("s3://b/ana/", [(utc(2026, 10, 2, 23, 30), NOW), (utc(2026, 9, 26, 21, 0), NOW)])]


async def test_live_counts_are_cached_for_five_minutes():
    b, storage, c = bidder(), Storage(), cache()
    await live_counts([b], NOW, SCHED, storage, c)
    await live_counts([b], NOW + timedelta(minutes=4), SCHED, storage, c)
    assert len(storage.calls) == 1
    await live_counts([b], NOW + timedelta(minutes=6), SCHED, storage, c)
    assert len(storage.calls) == 2


async def test_changing_the_folder_url_bypasses_the_cache():
    b, storage, c = bidder(), Storage(), cache()
    await live_counts([b], NOW, SCHED, storage, c)
    b.screenshotFolderUrl = "s3://b/ana-new/"
    await live_counts([b], NOW + timedelta(minutes=1), SCHED, storage, c)
    assert len(storage.calls) == 2 and storage.calls[-1][0] == "s3://b/ana-new/"


async def test_live_count_error_is_reported():
    b = bidder()
    got = await live_counts([b], NOW, SCHED, Storage(BidStorageError("NoSuchBucket")), cache())
    assert got == {str(b.id): {"today": None, "week": None, "error": "NoSuchBucket"}}


async def test_one_listing_per_bidder_for_both_windows():
    bidders, storage = [bidder("s3://b/ana/"), bidder("s3://b/raj/")], Storage(2)
    got = await live_counts(bidders, NOW, SCHED, storage, cache())
    assert sorted(url for url, _ in storage.calls) == ["s3://b/ana/", "s3://b/raj/"]
    assert all(len(windows) == 2 for _, windows in storage.calls)
    assert all(v == {"today": 2, "week": 2, "error": None} for v in got.values())


async def test_new_daily_window_relists_once_and_refreshes_both():
    b, storage, c = bidder(), Storage(), cache()
    before = utc(2026, 10, 2, 23, 28)  # Fri 18:28 CDT, two minutes before the daily cutoff
    await live_counts([b], before, SCHED, storage, c)
    storage.result = 7
    got = await live_counts([b], before + timedelta(minutes=3), SCHED, storage, c)  # week still cached
    assert len(storage.calls) == 2 and len(storage.calls[-1][1]) == 2
    assert got[str(b.id)] == {"today": 7, "week": 7, "error": None}


@pytest.fixture
async def db():
    await init_beanie(database=AsyncMongoMockClient()["bid-live-unit"], document_models=[Bidder, Bid])


async def seed_bids():
    owner = PydanticObjectId()
    ana = await Bidder(createdBy=owner, name="Ana", rate=0.5, screenshotFolderUrl="s3://b/ana/", folder="ana").insert()
    quiet = await Bidder(createdBy=owner, name="Raj", rate=0.5, screenshotFolderUrl="s3://b/raj/", folder="raj").insert()

    async def bid(name, at, status):
        await Bid(bidderId=ana.id, ownerId=owner, jobKey=f"ana/x/{name}", firstAt=at.replace(tzinfo=None), status=status).insert()

    await bid("today", utc(2026, 10, 3, 1, 0), "approved")       # after Fri 18:30 CDT: today and this week
    await bid("today-pending", utc(2026, 10, 3, 1, 5), "pending")
    await bid("earlier-in-week", utc(2026, 10, 1, 15, 0), "approved")  # after Sat 16:00 CDT, before today
    await bid("last-week", utc(2026, 9, 26, 20, 0), "approved")  # before the weekly cutoff
    return ana, quiet


async def test_live_counts_use_approved_records_when_both_windows_start_after_start_day(db, monkeypatch):
    records_start(monkeypatch, "2026-09-26")  # the open week starts Sat Sep 26 16:00 CDT, after that midnight
    ana, quiet = await seed_bids()
    storage = Storage()

    got = await live_counts([ana, quiet], NOW, SCHED, storage, cache())
    assert got == {str(ana.id): {"today": 1, "week": 2, "error": None}, str(quiet.id): {"today": 0, "week": 0, "error": None}}
    assert storage.calls == []


async def test_each_open_window_is_judged_by_its_own_start(db, monkeypatch):
    records_start(monkeypatch, "2026-10-01")  # today starts Oct 2 18:30 CDT (records); the week starts Sep 26 (folders)
    ana, quiet = await seed_bids()
    storage = Storage(9)

    got = await live_counts([ana, quiet], NOW, SCHED, storage, cache())
    assert got == {str(ana.id): {"today": 1, "week": 9, "error": None}, str(quiet.id): {"today": 0, "week": 9, "error": None}}
    # Only the week is listed from folders.
    assert sorted(storage.calls, key=lambda c: c[0]) == [
        ("s3://b/ana/", [(utc(2026, 9, 26, 21, 0), NOW)]), ("s3://b/raj/", [(utc(2026, 9, 26, 21, 0), NOW)]),
    ]


async def test_a_start_day_after_both_windows_began_keeps_folders(db, monkeypatch):
    records_start(monkeypatch, "2026-10-03")
    ana, _ = await seed_bids()
    storage = Storage(6)

    got = await live_counts([ana], NOW, SCHED, storage, cache())
    assert got == {str(ana.id): {"today": 6, "week": 6, "error": None}}
    assert len(storage.calls) == 1 and len(storage.calls[0][1]) == 2
