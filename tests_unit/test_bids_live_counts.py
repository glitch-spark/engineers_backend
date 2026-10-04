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
    """One `calls` entry per folder listing."""

    def __init__(self, result=4): self.result, self.calls = result, []

    async def count_windows(self, url, windows):
        self.calls.append((url, list(windows)))
        if isinstance(self.result, Exception):
            raise self.result
        return [self.result] * len(windows)


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
