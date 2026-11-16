import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from datetime import datetime, time, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from beanie import PydanticObjectId, init_beanie
from mongomock_motor import AsyncMongoMockClient

from app.bids import records, reports
from app.bids.reports import run_bid_reports_tick
from app.bids.slack_format import Owner
from app.bids.storage import BidStorageError
from app.bids.windows import BidSchedule, Rules
from app.config import get_settings
from app.models.bid import Bid
from app.models.bid_report import BidReport
from app.models.bid_report_post import BidReportPost
from app.models.bidder import Bidder

pytestmark = [  # pre-existing utcnow defaults (app/models/base.py) and Beanie internals, not bids code
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]

CT = ZoneInfo("America/Chicago")
SCHED = BidSchedule(tz=CT, daily_at=time(18, 30), weekly_day=5, weekly_at=time(16, 0))
DAILY_CUTOFF = datetime(2026, 10, 2, 23, 30, tzinfo=timezone.utc)   # Fri 18:30 CDT
OWNER = PydanticObjectId()


def bidder(name, folder, rate=0.5):
    return SimpleNamespace(id=PydanticObjectId(), createdBy=OWNER, name=name, country="BR", profileId=None,
                           rate=rate, screenshotFolderUrl=folder)


def _aware(dt): return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class FakeStore:
    def __init__(self, bidders):
        self.bidders, self.rows, self.done, self.posts = bidders, [], set(), {}

    async def claim(self, key):
        if key in self.done:
            return False
        self.done.add(key)
        return True
    async def release(self, key): self.done.discard(key)
    async def load_rows(self, kind, pk): return [r for r in self.rows if (r.kind, r.periodKey) == (kind, pk)]
    async def save_rows(self, rows): self.rows += rows; return rows
    async def overlapping_weekly(self, start, end, exclude_key):
        for r in self.rows:
            if (r.kind == "weekly" and r.periodKey != exclude_key and _aware(r.periodStart) < end
                    and _aware(r.periodEnd) > start):
                return r.periodKey
        return None
    async def active_bidders(self): return self.bidders
    async def profile_names(self, ids): return {}
    async def owners(self, ids): return {str(OWNER): Owner("Pedro", "Pedro")}
    async def save_post(self, kind, pk, channel, ts):
        self.posts[(kind, pk)] = SimpleNamespace(kind=kind, periodKey=pk, channel=channel, ts=ts, rev=0, dirty=False)
    async def dirty_posts(self): return [p for p in self.posts.values() if p.dirty]
    async def mark_synced(self, post, ts): post.ts, post.dirty = ts, False
    async def drop_post(self, post): del self.posts[(post.kind, post.periodKey)]


class FakeStorage:
    def __init__(self, counts): self.counts, self.calls = counts, 0

    async def count_bids(self, url, start, end):
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
        return "C1", [f"{len(self.sent)}.0"]


class Editor:
    def __init__(self, error=None): self.error, self.calls = error, []

    async def __call__(self, channel, ts, text, messages):
        if self.error:
            raise RuntimeError(self.error)
        self.calls.append((channel, ts, text))
        return ts


async def tick(now, store, storage, post, edit=None):
    return await run_bid_reports_tick(now, store=store, storage=storage, post=post, edit=edit, sched=SCHED)


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
    assert (got["daily"], got["weekly"]) == ("posted", "posted")
    assert {"bids-daily:2026-10-03", "bids-weekly:2026-10-03"} <= store.done


def test_cron_endpoint_rejects_wrong_secret(monkeypatch):
    from fastapi import HTTPException
    from app.routers import bidders
    monkeypatch.setattr(bidders, "get_settings", lambda: SimpleNamespace(SLACK_BOT_CRON_SECRET="s3cret"))
    with pytest.raises(HTTPException) as exc:
        import asyncio
        asyncio.run(bidders.bids_tick(x_cron_secret="wrong"))
    assert exc.value.status_code == 401


# --- build_rows: approved bid records (from BID_RECORDS_START when set), folders only before that day ---

START, END = datetime(2026, 10, 1, 23, 30, tzinfo=timezone.utc), DAILY_CUTOFF  # the Thursday-evening to Friday-evening day


@pytest.fixture
async def db():
    database = AsyncMongoMockClient()["bid-reports-unit"]
    await init_beanie(database=database, document_models=[Bidder, Bid, BidReport, BidReportPost])
    return database


@pytest.fixture(autouse=True)
def no_records_start(monkeypatch):
    """A local .env must not change which source a test counts. Default here: every window predates the start day,
    so the folder-path tests count folders; record tests set their own start (unset = bid records everywhere)."""
    records_start(monkeypatch, "2099-01-01")


def records_start(monkeypatch, value):
    monkeypatch.setattr(records, "get_settings", lambda: get_settings().model_copy(update={"BID_RECORDS_START": value}))


async def stored_bidder(name="Ana", rate=0.5):
    return await Bidder(createdBy=OWNER, name=name, rate=rate, country="BR", screenshotFolderUrl=f"s3://b/{name}/",
                        folder=name).insert()


async def add_bid(b, name, status, at=START + timedelta(hours=3)):
    await Bid(bidderId=b.id, ownerId=OWNER, jobKey=f"{b.folder}/2026-10-02/backend/{name}", status=status,
              firstAt=at.replace(tzinfo=None)).insert()


async def test_build_rows_counts_approved_records_after_start(db, monkeypatch):
    records_start(monkeypatch, "2026-10-01")  # the window starts Thu Oct 1 18:30 CDT, after the start day began
    ana = await stored_bidder(rate=0.1)
    for name, status in (("j1", "approved"), ("j2", "approved"), ("j3", "pending"), ("j4", "rejected")):
        await add_bid(ana, name, status)
    storage = FakeStorage({})  # any folder listing would KeyError

    (row,) = await reports.build_rows("daily", START, END, "2026-10-02", [ana], {}, storage, SCHED)
    assert (row.count, row.pending, row.rejected) == (2, 1, 1)
    assert row.amount == round(2 * 0.1, 2) == 0.2 and row.rate == 0.1 and row.error is None
    assert storage.calls == 0


async def test_build_rows_counts_records_when_start_is_unset(db, monkeypatch):
    records_start(monkeypatch, None)
    ana = await stored_bidder(rate=0.5)
    await add_bid(ana, "j1", "approved")
    await add_bid(ana, "j2", "pending")
    storage = FakeStorage({})  # any folder listing would KeyError

    (row,) = await reports.build_rows("daily", START, END, "2026-10-02", [ana], {}, storage, SCHED)
    assert (row.count, row.pending, row.rejected, row.amount) == (1, 1, 0, 0.5)
    assert storage.calls == 0


# a start day that straddles the window (it starts Oct 1 18:30 CDT, so Oct 2 midnight falls inside it);
# a start day after the window ended
@pytest.mark.parametrize("start_day", ["2026-10-02", "2026-10-03"])
async def test_build_rows_uses_folders_before_start(db, monkeypatch, start_day):
    records_start(monkeypatch, start_day)
    ana = await stored_bidder()
    await add_bid(ana, "j1", "approved")  # a record exists but is not what counts
    storage = FakeStorage({ana.screenshotFolderUrl: 5})

    (row,) = await reports.build_rows("daily", START, END, "2026-10-02", [ana], {}, storage, SCHED)
    assert (row.count, row.pending, row.rejected, row.amount) == (5, None, None, 2.5)
    assert storage.calls == 1


async def test_tick_counts_records_end_to_end(db, monkeypatch):
    records_start(monkeypatch, "2026-10-01")
    ana = await stored_bidder()
    await add_bid(ana, "j1", "approved")
    await add_bid(ana, "j2", "pending")
    store, post = FakeStore([ana]), Poster()
    got = await tick(DAILY_CUTOFF + timedelta(minutes=5), store, FakeStorage({}), post)
    assert got["daily"] == "posted"
    assert (store.rows[0].count, store.rows[0].pending, store.rows[0].rejected) == (1, 1, 0)


# --- the posted report is edited in place once a review recounts its rows ---

async def test_post_remembers_where_the_report_lives():
    store = FakeStore([bidder("Ana", "s3://b/ana/")])
    await tick(DAILY_CUTOFF + timedelta(minutes=5), store, FakeStorage({"s3://b/ana/": 3}), Poster())
    post = store.posts[("daily", "2026-10-02")]
    assert (post.channel, post.ts, post.dirty) == ("C1", ["1.0"], False)


async def test_changed_report_is_edited_in_place_on_a_later_tick():
    store, post, edit = FakeStore([bidder("Ana", "s3://b/ana/")]), Poster(), Editor()
    await tick(DAILY_CUTOFF + timedelta(minutes=5), store, FakeStorage({"s3://b/ana/": 3}), post, edit)
    assert edit.calls == []  # nothing changed yet

    store.rows[0].count = 7  # a review recounted the row
    store.posts[("daily", "2026-10-02")].dirty = True
    later = DAILY_CUTOFF + timedelta(hours=7)  # past the catch-up window: edits still go out
    got = await tick(later, store, FakeStorage({}), post, edit)
    assert got["refreshed"] == 1 and len(post.sent) == 1  # no new message, no thread reply
    ((channel, ts, text),) = edit.calls
    assert (channel, ts) == ("C1", ["1.0"]) and text == "Daily bids — Fri Oct 2: 7 bids"
    assert store.posts[("daily", "2026-10-02")].dirty is False

    again = await tick(later + timedelta(minutes=5), store, FakeStorage({}), post, edit)
    assert again["refreshed"] == 0 and len(edit.calls) == 1


async def test_report_deleted_in_slack_is_forgotten():
    store, post = FakeStore([bidder("Ana", "s3://b/ana/")]), Poster()
    await tick(DAILY_CUTOFF + timedelta(minutes=5), store, FakeStorage({"s3://b/ana/": 3}), post)
    store.posts[("daily", "2026-10-02")].dirty = True
    got = await tick(DAILY_CUTOFF + timedelta(minutes=10), store, FakeStorage({}), post, Editor("message_not_found"))
    assert got["refreshed"] == 0 and store.posts == {}


async def test_failed_edit_is_retried_on_the_next_tick():
    store, post = FakeStore([bidder("Ana", "s3://b/ana/")]), Poster()
    await tick(DAILY_CUTOFF + timedelta(minutes=5), store, FakeStorage({"s3://b/ana/": 3}), post)
    store.posts[("daily", "2026-10-02")].dirty = True
    got = await tick(DAILY_CUTOFF + timedelta(minutes=10), store, FakeStorage({}), post, Editor("ratelimited"))
    assert got["refreshed"] == 0 and store.posts[("daily", "2026-10-02")].dirty is True
    edit = Editor()
    got = await tick(DAILY_CUTOFF + timedelta(minutes=15), store, FakeStorage({}), post, edit)
    assert got["refreshed"] == 1 and len(edit.calls) == 1


async def test_review_recount_marks_the_posted_report_changed(db, monkeypatch):
    records_start(monkeypatch, "2026-10-01")
    ana = await stored_bidder()
    await add_bid(ana, "j1", "approved")
    (row,) = await reports.build_rows("daily", START, END, "2026-10-02", [ana], {}, FakeStorage({}), SCHED)
    await reports.BeanieReportStore().save_rows([row])
    await reports.BeanieReportStore().save_post("daily", "2026-10-02", "C1", ["1.0"])

    await records.refresh_report_rows(await Bid.find_one(Bid.bidderId == ana.id))  # nothing changed
    post = await BidReportPost.find_one(BidReportPost.periodKey == "2026-10-02")
    assert (post.dirty, post.rev) == (False, 0)

    await add_bid(ana, "j2", "approved")
    await records.refresh_report_rows(await Bid.find_one(Bid.bidderId == ana.id))
    post = await BidReportPost.find_one(BidReportPost.periodKey == "2026-10-02")
    assert (post.dirty, post.rev) == (True, 1)
    assert (await BidReport.find_one(BidReport.bidderId == ana.id)).count == 2


async def test_mark_synced_keeps_a_report_changed_mid_edit(db):
    store = reports.BeanieReportStore()
    await store.save_post("daily", "2026-10-02", "C1", ["1.0"])
    coll = BidReportPost.get_motor_collection()
    await coll.update_one({}, {"$set": {"dirty": True}, "$inc": {"rev": 1}})
    (seen,) = await store.dirty_posts()
    await coll.update_one({}, {"$inc": {"rev": 1}})  # a review lands while the edit is in flight
    await store.mark_synced(seen, ["1.0"])
    assert [p.rev for p in await store.dirty_posts()] == [2]
    (seen,) = await store.dirty_posts()
    await store.mark_synced(seen, ["1.0", "2.0"])
    assert await store.dirty_posts() == []
    assert (await BidReportPost.find_one()).ts == ["1.0", "2.0"]


async def test_update_report_edits_each_message_and_fits_a_new_length(monkeypatch):
    calls = []

    async def fake_api(method, body):
        calls.append((method, body.get("ts"), body.get("thread_ts")))
        return {"ok": True, "ts": "9.0"}
    monkeypatch.setattr(reports, "_slack_api", fake_api)

    assert await reports.update_report("C1", ["1.0", "2.0"], "t", [[{"b": 1}]]) == ["1.0"]
    assert calls == [("chat.update", "1.0", None), ("chat.delete", "2.0", None)]

    calls.clear()
    assert await reports.update_report("C1", ["1.0"], "t", [[{"b": 1}], [{"b": 2}]]) == ["1.0", "9.0"]
    assert calls == [("chat.update", "1.0", None), ("chat.postMessage", None, "1.0")]


# --- the schedule switch: Sun 2026-11-22 20:00 CST, daily 18:30 -> 18:00, weekly Sat 16:00 -> Sun 20:00 ---

SWITCH_SCHED = BidSchedule(tz=CT, daily_at=time(18, 30), weekly_day=5, weekly_at=time(16, 0),
                           switch_at=datetime(2026, 11, 22, 20, 0, tzinfo=CT),
                           next_rules=Rules(time(18, 0), 6, time(20, 0)))


async def switch_tick(local, store=None):
    store = store or FakeStore([bidder("Ana", "s3://b/ana/")])
    post = Poster()
    got = await run_bid_reports_tick(local.replace(tzinfo=CT).astimezone(timezone.utc), store=store,
                                     storage=FakeStorage({"s3://b/ana/": 3}), post=post, sched=SWITCH_SCHED)
    return got, store, post


def weekly_rows(store):
    return [r for r in store.rows if r.kind == "weekly"]


async def test_switch_no_weekly_report_on_the_saturday_before():
    # Sat 11-21 16:05: the old Saturday cutoff no longer exists, the long week is still open
    got, store, post = await switch_tick(datetime(2026, 11, 21, 16, 5))
    assert got["weekly"] == "not-due" and weekly_rows(store) == []
    assert not any("Weekly" in t for t in post.sent)
    assert "bids-weekly:2026-11-21" not in store.done and "bids-weekly:2026-11-14" not in store.done


async def test_switch_weekly_report_posts_at_the_sunday_cutoff():
    got, store, post = await switch_tick(datetime(2026, 11, 22, 20, 5))
    assert got["weekly"] == "posted" and "bids-weekly:2026-11-22" in store.done
    rows = weekly_rows(store)
    assert rows and {r.periodKey for r in rows} == {"2026-11-22"}
    assert all(r.periodStart.replace(tzinfo=timezone.utc) == datetime(2026, 11, 14, 22, 0, tzinfo=timezone.utc) and
               r.periodEnd.replace(tzinfo=timezone.utc) == datetime(2026, 11, 23, 2, 0, tzinfo=timezone.utc)
               for r in rows)
    weekly = [t for t in post.sent if "Weekly bids & pay" in t]
    assert len(weekly) == 1 and "week ending Sun Nov 22" in weekly[0]


async def test_switch_day_report_posts_at_the_old_cutoff_on_the_switch_day():
    got, store, post = await switch_tick(datetime(2026, 11, 22, 18, 35))
    assert got["daily"] == "posted" and "bids-daily:2026-11-22" in store.done
    assert got["weekly"] == "not-due" and weekly_rows(store) == []
    daily = [r for r in store.rows if r.kind == "daily"]
    assert {r.periodKey for r in daily} == {"2026-11-22"}
    # the day ending at the old Sun 18:30 cutoff: Sat 18:30 -> Sun 18:30 CST
    assert {(r.periodStart.replace(tzinfo=timezone.utc), r.periodEnd.replace(tzinfo=timezone.utc)) for r in daily} == {
        (datetime(2026, 11, 22, 0, 30, tzinfo=timezone.utc), datetime(2026, 11, 23, 0, 30, tzinfo=timezone.utc))}


async def test_switch_daily_report_moves_to_the_new_time_after_the_switch():
    got, store, post = await switch_tick(datetime(2026, 11, 23, 18, 5))
    assert got["daily"] == "posted" and "bids-daily:2026-11-23" in store.done
    daily = [r for r in store.rows if r.kind == "daily"]
    assert {r.periodKey for r in daily} == {"2026-11-23"}
    start = {r.periodStart.replace(tzinfo=timezone.utc) for r in daily}
    end = {r.periodEnd.replace(tzinfo=timezone.utc) for r in daily}
    # Sun 18:30 -> Mon 18:00 CST
    assert start == {datetime(2026, 11, 23, 0, 30, tzinfo=timezone.utc)}
    assert end == {datetime(2026, 11, 24, 0, 0, tzinfo=timezone.utc)}


async def test_switch_nothing_new_before_the_first_new_daily_cutoff():
    # Mon 11-23 17:59: the old 18:30 cutoff would not be until later, the new 18:00 not yet; the latest
    # cutoff is still Sun 11-22 18:30, long past its catch-up window, and nothing is posted or saved.
    got, store, post = await switch_tick(datetime(2026, 11, 23, 17, 59))
    assert got["daily"] == "not-due" and got["weekly"] == "not-due"
    assert store.rows == [] and post.sent == []


async def test_switch_read_from_settings(monkeypatch):
    s = get_settings().model_copy(update={
        "BID_SCHEDULE_SWITCH_AT": "2026-11-22T20:00", "BID_NEXT_DAILY_REPORT_TIME": "18:00",
        "BID_NEXT_WEEKLY_REPORT_DAY": "sunday", "BID_NEXT_WEEKLY_REPORT_TIME": "20:00"})
    monkeypatch.setattr("app.bids.windows.get_settings", lambda: s)
    store, post = FakeStore([bidder("Ana", "s3://b/ana/")]), Poster()
    now = datetime(2026, 11, 22, 20, 5, tzinfo=CT).astimezone(timezone.utc)
    got = await run_bid_reports_tick(now, store=store, storage=FakeStorage({"s3://b/ana/": 3}), post=post)
    assert got["weekly"] == "posted" and "bids-weekly:2026-11-22" in store.done


# --- the double-report guard: a weekly window may not overlap a stored weekly report of another period ---

def stored_weekly(store, key, start, end):
    store.rows.append(BidReport.model_construct(
        kind="weekly", periodKey=key, periodStart=start, periodEnd=end, bidderId=PydanticObjectId(),
        ownerId=OWNER, bidderName="Ana", rate=0.5, count=1, amount=0.5))


async def test_switch_weekly_overlapping_a_stored_report_is_refused(caplog):
    # old code posted weekly 2026-11-21 (11-14 -> 11-21); the long week 11-14 -> 11-22 would pay those days twice
    store = FakeStore([bidder("Ana", "s3://b/ana/")])
    stored_weekly(store, "2026-11-21", datetime(2026, 11, 14, 22, 0, tzinfo=timezone.utc),
                  datetime(2026, 11, 21, 22, 0, tzinfo=timezone.utc))
    with caplog.at_level("ERROR", logger="bids.reports"):
        got, store, post = await switch_tick(datetime(2026, 11, 22, 20, 5), store)
    assert got["weekly"] == "overlap:2026-11-21"
    assert not any("Weekly" in t for t in post.sent)
    assert {r.periodKey for r in weekly_rows(store)} == {"2026-11-21"}
    assert "bids-weekly:2026-11-22" in store.done  # the claim stays: later ticks do not retry
    assert any("2026-11-22" in m and "2026-11-21" in m for m in caplog.messages)
    again = await run_bid_reports_tick(datetime(2026, 11, 22, 20, 10, tzinfo=CT).astimezone(timezone.utc),
                                       store=store, storage=FakeStorage({}), post=post, sched=SWITCH_SCHED)
    assert again["weekly"] == "done" and not any("Weekly" in t for t in post.sent)


async def test_switch_weekly_after_a_normal_previous_week_is_not_refused():
    store = FakeStore([bidder("Ana", "s3://b/ana/")])
    stored_weekly(store, "2026-11-14", datetime(2026, 11, 7, 22, 0, tzinfo=timezone.utc),
                  datetime(2026, 11, 14, 22, 0, tzinfo=timezone.utc))
    got, store, post = await switch_tick(datetime(2026, 11, 22, 20, 5), store)
    assert got["weekly"] == "posted"


async def test_overlap_guard_does_not_apply_to_daily_reports():
    store = FakeStore([bidder("Ana", "s3://b/ana/")])
    stored_weekly(store, "2026-11-21", datetime(2026, 11, 14, 22, 0, tzinfo=timezone.utc),
                  datetime(2026, 11, 21, 22, 0, tzinfo=timezone.utc))
    got, _, _ = await switch_tick(datetime(2026, 11, 22, 18, 35), store)
    assert got["daily"] == "posted"


async def test_real_store_finds_an_overlapping_weekly_report(db):
    start, end = datetime(2026, 11, 14, 22, 0, tzinfo=timezone.utc), datetime(2026, 11, 23, 2, 0, tzinfo=timezone.utc)
    store = reports.BeanieReportStore()
    for kind, key, s_, e_ in [
        ("weekly", "2026-11-14", datetime(2026, 11, 7, 22, 0), datetime(2026, 11, 14, 22, 0)),   # adjacent
        ("daily", "2026-11-20", datetime(2026, 11, 19, 23, 30), datetime(2026, 11, 20, 23, 30)),  # not weekly
    ]:
        await BidReport(kind=kind, periodKey=key, periodStart=s_, periodEnd=e_, bidderId=PydanticObjectId(),
                        ownerId=OWNER, bidderName="Ana", rate=0.5, count=1, amount=0.5).insert()
    assert await store.overlapping_weekly(start, end, "2026-11-22") is None
    await BidReport(kind="weekly", periodKey="2026-11-21", periodStart=datetime(2026, 11, 14, 22, 0),
                    periodEnd=datetime(2026, 11, 21, 22, 0), bidderId=PydanticObjectId(), ownerId=OWNER,
                    bidderName="Ana", rate=0.5, count=1, amount=0.5).insert()
    assert await store.overlapping_weekly(start, end, "2026-11-22") == "2026-11-21"
    assert await store.overlapping_weekly(start, end, "2026-11-21") is None
