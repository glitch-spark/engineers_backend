import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from datetime import datetime, time, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from beanie import PydanticObjectId

from app.bids.live_counts import TtlCache, live_counts
from app.bids.storage import BidStorageError
from app.bids.windows import BidSchedule

CT = ZoneInfo("America/Chicago")
SCHED = BidSchedule(tz=CT, daily_at=time(18, 30), weekly_day=5, weekly_at=time(16, 0))


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


NOW = utc(2026, 10, 3, 3, 0)  # Fri Oct 2 22:00 CDT


class Storage:
    def __init__(self, result=4): self.result, self.calls = result, []

    async def count_screenshots(self, url, start, end):
        self.calls.append((url, start, end))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def bidder(url="s3://b/ana/"):
    return SimpleNamespace(id=PydanticObjectId(), screenshotFolderUrl=url)


def cache():
    return TtlCache(timedelta(minutes=5))


async def test_live_counts_use_open_windows():
    b, storage = bidder(), Storage()
    got = await live_counts([b], NOW, SCHED, storage, cache())
    assert got == {str(b.id): {"today": 4, "week": 4, "error": None}}
    assert sorted((s, e) for _, s, e in storage.calls) == [(utc(2026, 9, 26, 21, 0), NOW), (utc(2026, 10, 2, 23, 30), NOW)]


async def test_live_counts_are_cached_for_five_minutes():
    b, storage, c = bidder(), Storage(), cache()
    await live_counts([b], NOW, SCHED, storage, c)
    await live_counts([b], NOW + timedelta(minutes=4), SCHED, storage, c)
    assert len(storage.calls) == 2
    await live_counts([b], NOW + timedelta(minutes=6), SCHED, storage, c)
    assert len(storage.calls) == 4


async def test_changing_the_folder_url_bypasses_the_cache():
    b, storage, c = bidder(), Storage(), cache()
    await live_counts([b], NOW, SCHED, storage, c)
    b.screenshotFolderUrl = "s3://b/ana-new/"
    await live_counts([b], NOW + timedelta(minutes=1), SCHED, storage, c)
    assert len(storage.calls) == 4 and storage.calls[-1][0] == "s3://b/ana-new/"


async def test_live_count_error_is_reported():
    b = bidder()
    got = await live_counts([b], NOW, SCHED, Storage(BidStorageError("NoSuchBucket")), cache())
    assert got == {str(b.id): {"today": None, "week": None, "error": "NoSuchBucket"}}
