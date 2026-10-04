import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from datetime import datetime, time, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from beanie import PydanticObjectId

from app.bids import reports
from app.bids.reports import run_bid_reports_tick
from app.bids.slack_format import Owner
from app.bids.storage import BidStorageError
from app.bids.windows import BidSchedule

CT = ZoneInfo("America/Chicago")
SCHED = BidSchedule(tz=CT, daily_at=time(18, 30), weekly_day=5, weekly_at=time(16, 0))
DAILY_CUTOFF = datetime(2026, 10, 2, 23, 30, tzinfo=timezone.utc)   # Fri 18:30 CDT
OWNER = PydanticObjectId()


def bidder(name, folder, rate=0.5):
    return SimpleNamespace(id=PydanticObjectId(), createdBy=OWNER, name=name, country="BR", profileId=None,
                           rate=rate, screenshotFolderUrl=folder)


class FakeStore:
    def __init__(self, bidders):
        self.bidders, self.rows, self.done = bidders, [], set()

    async def claim(self, key):
        if key in self.done:
            return False
        self.done.add(key)
        return True
    async def release(self, key): self.done.discard(key)
    async def load_rows(self, kind, pk): return [r for r in self.rows if (r.kind, r.periodKey) == (kind, pk)]
    async def save_rows(self, rows): self.rows += rows; return rows
    async def active_bidders(self): return self.bidders
    async def profile_names(self, ids): return {}
    async def owners(self, ids): return {str(OWNER): Owner("Pedro", "Pedro")}


class FakeStorage:
    def __init__(self, counts): self.counts, self.calls = counts, 0

    async def count_screenshots(self, url, start, end):
        self.calls += 1
        c = self.counts[url]
        if isinstance(c, Exception):
            raise c
        return c


class Poster:
    def __init__(self, fail=False): self.fail, self.sent = fail, []

    async def __call__(self, text, messages):
        if self.fail:
            raise RuntimeError("slack down")
        self.sent.append(text)


async def tick(now, store, storage, post):
    return await run_bid_reports_tick(now, store=store, storage=storage, post=post, sched=SCHED)


async def test_not_due_after_catch_up_window():
    store, post = FakeStore([bidder("Ana", "s3://b/ana/")]), Poster()
    got = await tick(DAILY_CUTOFF + timedelta(hours=6, minutes=1), store, FakeStorage({"s3://b/ana/": 3}), post)
    assert got["daily"] == "not-due" and store.rows == [] and post.sent == []


async def test_due_daily_saves_rows_posts_and_marks_done():
    store, storage, post = FakeStore([bidder("Ana", "s3://b/ana/"), bidder("Raj", "s3://b/raj/")]), \
        FakeStorage({"s3://b/ana/": 3, "s3://b/raj/": 0}), Poster()
    got = await tick(DAILY_CUTOFF + timedelta(minutes=5), store, storage, post)
    assert got["daily"] == "posted"
    assert sorted((r.bidderName, r.count) for r in store.rows) == [("Ana", 3), ("Raj", 0)]
    assert "bids-daily:2026-10-02" in store.done and len(post.sent) == 1
    again = await tick(DAILY_CUTOFF + timedelta(minutes=10), store, storage, post)
    assert again["daily"] == "done" and storage.calls == 2 and len(post.sent) == 1


async def test_failed_post_is_retried_with_saved_rows():
    store, storage = FakeStore([bidder("Ana", "s3://b/ana/")]), FakeStorage({"s3://b/ana/": 3})
    first = await tick(DAILY_CUTOFF + timedelta(minutes=5), store, storage, Poster(fail=True))
    assert first["daily"] == "failed:post" and store.done == set() and len(store.rows) == 1
    post = Poster()
    second = await tick(DAILY_CUTOFF + timedelta(minutes=10), store, storage, post)
    assert second["daily"] == "posted" and storage.calls == 1 and len(store.rows) == 1


async def test_one_unreadable_folder_does_not_stop_the_report():
    store = FakeStore([bidder("Ana", "s3://b/ana/"), bidder("John", "s3://b/john/")])
    storage = FakeStorage({"s3://b/ana/": 3, "s3://b/john/": BidStorageError("AccessDenied")})
    got = await tick(DAILY_CUTOFF + timedelta(minutes=5), store, storage, Poster())
    john = next(r for r in store.rows if r.bidderName == "John")
    assert got["daily"] == "posted" and john.count is None and john.error == "AccessDenied" and john.amount is None


async def test_no_bidders_marks_done_without_posting():
    store, post = FakeStore([]), Poster()
    got = await tick(DAILY_CUTOFF + timedelta(minutes=5), store, FakeStorage({}), post)
    assert got["daily"] == "no-bidders" and "bids-daily:2026-10-02" in store.done and post.sent == []


async def test_overlapping_tick_does_not_post_twice():
    store, storage, post = FakeStore([bidder("Ana", "s3://b/ana/")]), FakeStorage({"s3://b/ana/": 3}), Poster()
    store.done.add("bids-daily:2026-10-02")  # another run already claimed this period
    got = await tick(DAILY_CUTOFF + timedelta(minutes=5), store, storage, post)
    assert got["daily"] == "done" and post.sent == [] and storage.calls == 0 and store.rows == []


async def test_slack_not_configured_saves_but_does_not_mark(monkeypatch):
    monkeypatch.setattr(reports, "bids_slack_configured", lambda: False)
    store = FakeStore([bidder("Ana", "s3://b/ana/")])
    got = await tick(DAILY_CUTOFF + timedelta(minutes=5), store, FakeStorage({"s3://b/ana/": 3}), None)
    assert got["daily"] == "saved:slack-not-configured" and len(store.rows) == 1 and store.done == set()


async def test_amount_is_rounded_rate_snapshot():
    store = FakeStore([bidder("Ana", "s3://b/ana/", rate=0.1)])
    await tick(DAILY_CUTOFF + timedelta(minutes=5), store, FakeStorage({"s3://b/ana/": 3}), Poster())
    assert store.rows[0].rate == 0.1 and store.rows[0].amount == 0.3


async def test_saturday_evening_runs_daily_and_weekly():
    # Sat 2026-10-03 18:35 CDT: daily cutoff 5 min ago, weekly cutoff (16:00) 2h35m ago
    now = datetime(2026, 10, 3, 23, 35, tzinfo=timezone.utc)
    store, post = FakeStore([bidder("Ana", "s3://b/ana/")]), Poster()
    got = await tick(now, store, FakeStorage({"s3://b/ana/": 3}), post)
    assert got == {"daily": "posted", "weekly": "posted"}
    assert {"bids-daily:2026-10-03", "bids-weekly:2026-10-03"} <= store.done


def test_cron_endpoint_rejects_wrong_secret(monkeypatch):
    from fastapi import HTTPException
    from app.routers import bidders
    monkeypatch.setattr(bidders, "get_settings", lambda: SimpleNamespace(SLACK_BOT_CRON_SECRET="s3cret"))
    with pytest.raises(HTTPException) as exc:
        import asyncio
        asyncio.run(bidders.bids_tick(x_cron_secret="wrong"))
    assert exc.value.status_code == 401
