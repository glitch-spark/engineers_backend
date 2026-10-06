"""Bid review routes (/bids) against an in-memory MongoDB (mongomock-motor); no real database."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from datetime import date, datetime, timedelta, timezone

import pytest
from beanie import PydanticObjectId, init_beanie
from mongomock_motor import AsyncMongoMockClient

from app.bids.storage import BidStorageError
from app.bids.windows import latest_cutoff, period_key, schedule_from_settings, window_for
from app.models import Account, UserExtra
from app.models.bid import Bid, Screenshot
from app.models.bid_report import BidReport
from app.models.bidder import Bidder
from app.models.slack_alert_log import SlackAlertLog

pytestmark = [
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]

DAY = "2026-10-05"


def day_window(day: str = DAY):
    sched = schedule_from_settings()
    cutoff = datetime.combine(date.fromisoformat(day), sched.daily_at, tzinfo=sched.tz)
    return window_for("daily", cutoff, sched)


def iso(at: datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%S") + "Z"


class FakeStorage:
    def __init__(self, folders=0, error=None):
        self.folders, self.error, self.signed, self.listed = folders, error, [], []

    async def count_bids(self, folder_url, start, end):
        self.listed.append(folder_url)
        if self.error:
            raise self.error
        return self.folders

    def presign_get(self, bucket, key):
        self.signed.append((bucket, key))
        return f"https://signed/{bucket}/{key}"


@pytest.fixture
async def db():
    database = AsyncMongoMockClient()["review-unit"]
    await init_beanie(database=database, document_models=[Bidder, Bid, BidReport, Account, UserExtra, SlackAlertLog])
    return database


@pytest.fixture
def storage(monkeypatch):
    from app.routers import bids

    fake = FakeStorage()
    monkeypatch.setattr(bids, "get_bid_storage", lambda: fake)
    monkeypatch.setattr(bids, "FOLDER_CACHE", bids.TtlCache(timedelta(minutes=5)))
    return fake


async def make_user(name, role="staff") -> UserExtra:
    return await UserExtra(name=name, email=f"{name.lower()}@x.io", role=role).insert()


@pytest.fixture
async def owner(db):
    return await make_user("Pedro")


@pytest.fixture
def make_client(db, monkeypatch, storage):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.config import get_settings
    from app.deps import get_current_user
    from app.routers import bids

    monkeypatch.setenv("BID_STORAGE_BUCKET", "bid-screenshots")
    get_settings.cache_clear()

    def make(user):
        app = FastAPI()
        app.include_router(bids.router)
        app.dependency_overrides[get_current_user] = lambda: user
        return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")

    yield make
    get_settings.cache_clear()


async def make_bidder(owner, name="Ana", archived=False) -> Bidder:
    return await Bidder(
        createdBy=owner.id, name=name, rate=0.5, folder=name.lower(),
        screenshotFolderUrl=f"s3://shots/{name.lower()}/",
        archivedAt=datetime.utcnow() if archived else None,
    ).insert()


async def add_bid(bidder, job, at, status="pending", uploaded=True, **kw) -> Bid:
    naive = at.astimezone(timezone.utc).replace(tzinfo=None)
    shots = [  # stored out of step order on purpose
        Screenshot(key=f"{bidder.folder}/{DAY}/backend/{job}/step1_step_1.png", step=2, trigger="step",
                   capturedAt=naive, uploadedAt=naive if uploaded else None),
        Screenshot(key=f"{bidder.folder}/{DAY}/backend/{job}/step0_step_0.png", step=1, trigger="step",
                   capturedAt=naive, uploadedAt=None),
    ]
    return await Bid(
        bidderId=bidder.id, ownerId=bidder.createdBy, jobKey=f"{bidder.folder}/{DAY}/backend/{job}",
        jobUrl=f"https://jobs.example/{job}", jobTitle=job.title(), firstAt=naive, status=status,
        screenshots=shots, **kw,
    ).insert()


async def add_report(bidder, owner, pending, rejected=0) -> BidReport:
    start, end = day_window()
    return await BidReport(
        kind="daily", periodKey=DAY, periodStart=start.replace(tzinfo=None), periodEnd=end.replace(tzinfo=None),
        bidderId=bidder.id, ownerId=owner.id, bidderName=bidder.name, rate=0.5, count=0, amount=0.0,
        pending=pending, rejected=rejected,
    ).insert()


async def test_owner_lists_their_bids_for_the_day(make_client, owner, storage):
    start, end = day_window()
    ana, ben = await make_bidder(owner, "Ana"), await make_bidder(owner, "Ben")
    stranger = await make_bidder(await make_user("Other"), "Zed")
    storage.folders = 4
    await add_bid(ana, "one", start + timedelta(hours=1), "approved")
    older = await add_bid(ana, "two", start + timedelta(hours=2), "pending", uploaded=False)
    await add_bid(ana, "three", start + timedelta(hours=3), "rejected")
    await add_bid(ben, "four", start + timedelta(hours=4), "approved")
    await add_bid(ana, "next-window", end + timedelta(hours=1), "approved")
    await add_bid(stranger, "foreign", start + timedelta(hours=1), "approved")

    async with make_client(owner) as c:
        r = await c.get("/bids", params={"day": DAY})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["day"] == DAY
    assert (body["start"], body["end"]) == (iso(start), iso(end))
    assert [b["jobTitle"] for b in body["bids"]] == ["Four", "Three", "Two", "One"]  # newest first
    assert body["summary"][str(ana.id)] == {
        "name": "Ana", "approved": 1, "pending": 1, "rejected": 1, "foldersWithoutRecord": 1,  # 4 folders - 3 bids
    }
    assert body["summary"][str(ben.id)] == {
        "name": "Ben", "approved": 1, "pending": 0, "rejected": 0, "foldersWithoutRecord": 3,
    }
    assert str(stranger.id) not in body["summary"]
    two = next(b for b in body["bids"] if b["id"] == str(older.id))
    assert two == {
        "id": str(older.id), "bidderId": str(ana.id), "bidderName": "Ana", "jobUrl": "https://jobs.example/two",
        "jobTitle": "Two", "firstAt": iso(start + timedelta(hours=2)),
        "submittedAt": None, "screenshotCount": 2, "missingUploads": 2, "status": "pending",
        "reviewedByName": None, "reviewedAt": None, "note": None, "changedSinceReview": False,
    }


async def test_list_filters_by_status_and_bidder_but_summary_stays_whole(make_client, owner):
    start, _ = day_window()
    ana, ben = await make_bidder(owner, "Ana"), await make_bidder(owner, "Ben")
    await add_bid(ana, "one", start + timedelta(hours=1), "approved")
    await add_bid(ana, "two", start + timedelta(hours=2), "pending")
    await add_bid(ben, "three", start + timedelta(hours=3), "pending")
    async with make_client(owner) as c:
        pending = (await c.get("/bids", params={"day": DAY, "status": "pending"})).json()
        mine = (await c.get("/bids", params={"day": DAY, "bidderId": str(ana.id)})).json()
        gone = await c.get("/bids", params={"day": DAY, "bidderId": str(PydanticObjectId())})
    assert {b["jobTitle"] for b in pending["bids"]} == {"Two", "Three"}
    assert pending["summary"][str(ana.id)]["approved"] == 1
    assert {b["jobTitle"] for b in mine["bids"]} == {"One", "Two"}
    assert list(mine["summary"]) == [str(ana.id)]
    assert gone.status_code == 404


async def test_folders_without_record_is_null_when_unreadable_and_never_negative(make_client, owner, storage):
    start, _ = day_window()
    ana = await make_bidder(owner, "Ana")
    await add_bid(ana, "one", start + timedelta(hours=1))
    await add_bid(ana, "two", start + timedelta(hours=2))
    async with make_client(owner) as c:
        storage.folders = 1  # fewer folders than records
        assert (await c.get("/bids", params={"day": DAY})).json()["summary"][str(ana.id)]["foldersWithoutRecord"] == 0
        ben = await make_bidder(owner, "Ben")
        storage.error = BidStorageError("AccessDenied")
        summary = (await c.get("/bids", params={"day": DAY})).json()["summary"]
    assert summary[str(ben.id)]["foldersWithoutRecord"] is None


async def test_folder_listing_is_cached_between_requests(make_client, owner, storage):
    start, _ = day_window()
    await add_bid(await make_bidder(owner), "one", start + timedelta(hours=1))
    async with make_client(owner) as c:
        for _ in range(3):
            assert (await c.get("/bids", params={"day": DAY})).status_code == 200
    assert len(storage.listed) == 1


async def test_admin_sees_every_bidder(make_client, owner):
    start, _ = day_window()
    admin = await make_user("Root", "admin")
    ana = await make_bidder(owner, "Ana")
    other = await make_bidder(await make_user("Other"), "Zed")
    archived = await make_bidder(owner, "Old", archived=True)
    idle_archived = await make_bidder(owner, "Idle", archived=True)
    for bidder, job in [(ana, "a"), (other, "z"), (archived, "o")]:
        await add_bid(bidder, job, start + timedelta(hours=1))
    async with make_client(admin) as c:
        body = (await c.get("/bids", params={"day": DAY})).json()
    assert len(body["bids"]) == 3
    assert set(body["summary"]) == {str(ana.id), str(other.id), str(archived.id)}
    assert str(idle_archived.id) not in body["summary"]  # archived and nothing that day


async def test_other_user_gets_404_on_review(make_client, owner):
    start, _ = day_window()
    bid = await add_bid(await make_bidder(owner), "one", start + timedelta(hours=1))
    stranger = await make_user("Other")
    async with make_client(stranger) as c:
        shots = await c.get(f"/bids/{bid.id}/screenshots")
        put = await c.put(f"/bids/{bid.id}/review", json={"status": "approved"})
        missing = await c.put(f"/bids/{PydanticObjectId()}/review", json={"status": "approved"})
        bad_id = await c.put("/bids/not-an-id/review", json={"status": "approved"})
    assert [r.status_code for r in (shots, put, missing, bad_id)] == [404] * 4
    assert put.json()["detail"] == "bid not found"
    assert (await Bid.get(bid.id)).status == "pending"


async def test_review_sets_status_and_refreshes_reports(make_client, owner):
    start, _ = day_window()
    ana = await make_bidder(owner)
    bid = await add_bid(ana, "one", start + timedelta(hours=1), "rejected", changedSinceReview=True,
                        reviewedByName="Someone", note="blurry")
    await add_report(ana, owner, pending=0, rejected=1)
    async with make_client(owner) as c:
        r = await c.put(f"/bids/{bid.id}/review", json={"status": "approved", "note": "  fine  "})
    assert r.status_code == 200, r.text
    out = r.json()
    assert (out["status"], out["reviewedByName"], out["note"], out["changedSinceReview"]) == (
        "approved", "Pedro", "fine", False)
    assert out["reviewedAt"].endswith("Z")
    stored = await Bid.get(bid.id)
    assert (stored.status, stored.reviewedBy, stored.changedSinceReview) == ("approved", owner.id, False)
    row = await BidReport.find_one(BidReport.bidderId == ana.id)
    assert (row.count, row.pending, row.rejected, row.amount) == (1, 0, 0, 0.5)


async def test_review_validates_status_and_note_length(make_client, owner):
    start, _ = day_window()
    bid = await add_bid(await make_bidder(owner), "one", start + timedelta(hours=1))
    async with make_client(owner) as c:
        bad = await c.put(f"/bids/{bid.id}/review", json={"status": "maybe"})
        long = await c.put(f"/bids/{bid.id}/review", json={"status": "rejected", "note": "x" * 501})
        ok = await c.put(f"/bids/{bid.id}/review", json={"status": "rejected", "note": "x" * 500})
        blank = await c.put(f"/bids/{bid.id}/review", json={"status": "pending", "note": "   "})
    assert (bad.status_code, long.status_code, ok.status_code) == (422, 422, 200)
    assert blank.json()["note"] is None and blank.json()["status"] == "pending"


async def test_archived_bidders_bids_stay_reviewable(make_client, owner):
    start, _ = day_window()
    bid = await add_bid(await make_bidder(owner, archived=True), "one", start + timedelta(hours=1))
    async with make_client(owner) as c:
        r = await c.put(f"/bids/{bid.id}/review", json={"status": "approved"})
    assert r.status_code == 200


async def test_bulk_review_skips_foreign_ids(make_client, owner):
    start, _ = day_window()
    ana = await make_bidder(owner)
    stranger = await make_bidder(await make_user("Other"), "Zed")
    mine = [await add_bid(ana, job, start + timedelta(hours=h)) for h, job in [(1, "a"), (2, "b")]]
    foreign = await add_bid(stranger, "z", start + timedelta(hours=1))
    await add_report(ana, owner, pending=2)
    ids = [str(b.id) for b in mine] + [str(foreign.id)]
    async with make_client(owner) as c:
        r = await c.post("/bids/review", json={"ids": ids, "status": "approved"})
        junk = await c.post("/bids/review", json={"ids": ["nope", str(PydanticObjectId())], "status": "approved"})
        empty = await c.post("/bids/review", json={"ids": [], "status": "approved"})
        many = await c.post("/bids/review", json={"ids": [str(PydanticObjectId())] * 501, "status": "approved"})
    assert r.json() == {"updated": 2, "skipped": 1}
    assert junk.json() == {"updated": 0, "skipped": 2}
    assert (empty.status_code, many.status_code) == (422, 422)
    assert [b.status for b in await Bid.find({"bidderId": ana.id}).to_list()] == ["approved", "approved"]
    assert (await Bid.get(foreign.id)).status == "pending"
    row = await BidReport.find_one(BidReport.bidderId == ana.id)
    assert (row.count, row.pending) == (2, 0)
    assert (await Bid.get(mine[0].id)).reviewedByName == "Pedro"


async def test_bulk_review_clears_changed_since_review_and_counts_repeated_ids_once(make_client, owner):
    start, _ = day_window()
    bid = await add_bid(await make_bidder(owner), "a", start + timedelta(hours=1), "approved", changedSinceReview=True)
    async with make_client(owner) as c:
        r = await c.post("/bids/review", json={"ids": [str(bid.id)] * 2, "status": "approved"})
    assert r.json() == {"updated": 1, "skipped": 0}
    assert (await Bid.get(bid.id)).changedSinceReview is False


async def test_bulk_review_refreshes_once_per_bidder_and_day(make_client, owner, monkeypatch):
    from app.routers import bids

    start, end = day_window()
    ana, ben = await make_bidder(owner, "Ana"), await make_bidder(owner, "Ben")
    created = [
        await add_bid(ana, "a", start + timedelta(hours=1)), await add_bid(ana, "b", start + timedelta(hours=2)),
        await add_bid(ben, "c", start + timedelta(hours=3)),
        await add_bid(ana, "d", end + timedelta(hours=2)),  # the next day
    ]
    refreshed = []

    async def spy(bid):
        refreshed.append(bid.bidderId)

    monkeypatch.setattr(bids, "refresh_report_rows", spy)
    async with make_client(owner) as c:
        r = await c.post("/bids/review", json={"ids": [str(b.id) for b in created], "status": "rejected"})
    assert r.json() == {"updated": 4, "skipped": 0}
    assert sorted(map(str, refreshed)) == sorted(map(str, [ana.id, ana.id, ben.id]))


async def test_screenshots_only_sign_uploaded_ones(make_client, owner, storage):
    start, _ = day_window()
    bid = await add_bid(await make_bidder(owner), "one", start + timedelta(hours=1))
    async with make_client(owner) as c:
        r = await c.get(f"/bids/{bid.id}/screenshots")
    assert r.status_code == 200, r.text
    shots = r.json()
    assert [s["step"] for s in shots] == [1, 2]  # step order, not stored order
    assert shots[0] == {
        "key": "ana/2026-10-05/backend/one/step0_step_0.png", "step": 1, "trigger": "step",
        "capturedAt": iso(start + timedelta(hours=1)), "url": None,  # never confirmed
    }
    assert shots[1]["url"] == "https://signed/bid-screenshots/ana/2026-10-05/backend/one/step1_step_1.png"
    assert storage.signed == [("bid-screenshots", "ana/2026-10-05/backend/one/step1_step_1.png")]


async def test_screenshots_503_when_signing_fails_or_storage_missing(make_client, owner, storage, monkeypatch):
    from app.config import get_settings

    start, _ = day_window()
    bid = await add_bid(await make_bidder(owner), "one", start + timedelta(hours=1))

    def broken(bucket, key):
        raise BidStorageError("bad endpoint")

    monkeypatch.setattr(storage, "presign_get", broken)
    async with make_client(owner) as c:
        assert (await c.get(f"/bids/{bid.id}/screenshots")).status_code == 503
        monkeypatch.setenv("BID_STORAGE_BUCKET", "")
        get_settings.cache_clear()
        assert (await c.get(f"/bids/{bid.id}/screenshots")).status_code == 503


async def test_invalid_day_is_400(make_client, owner):
    async with make_client(owner) as c:
        for day in ["2026-13-01", "yesterday", "2026-10-5", "20261005", "0001-01-01", ""]:
            r = await c.get("/bids", params={"day": day})
            assert r.status_code == 400, day
            assert r.json()["detail"] == "Invalid day"


async def test_day_defaults_to_the_open_window(make_client, owner):
    sched = schedule_from_settings()
    before = datetime.now(timezone.utc)
    async with make_client(owner) as c:
        body = (await c.get("/bids")).json()
    after = datetime.now(timezone.utc)
    # The open window is [latest cutoff, next cutoff); its report is the one posted at the next cutoff.
    expected = {
        period_key(datetime.combine(latest_cutoff("daily", now, sched).date() + timedelta(days=1), sched.daily_at,
                                    tzinfo=sched.tz))
        for now in (before, after)
    }
    assert body["day"] in expected
    start, end = day_window(body["day"])
    assert body["start"] == iso(start)
    assert start <= before < end


async def test_bulk_review_refreshes_both_weekly_rows_when_a_day_straddles_the_weekly_cutoff(make_client, owner):
    sched = schedule_from_settings()
    weekly = datetime.combine(date(2026, 10, 5) + timedelta(days=sched.weekly_day), sched.weekly_at, tzinfo=sched.tz)
    before, after = weekly - timedelta(hours=1), weekly + timedelta(hours=1)
    assert latest_cutoff("daily", before, sched) == latest_cutoff("daily", after, sched)  # one daily window
    daily_start, daily_end = window_for("daily", latest_cutoff("daily", before, sched) + timedelta(days=1), sched)
    ana = await make_bidder(owner)
    early = await add_bid(ana, "early", before)
    late = await add_bid(ana, "late", after)

    def row(kind, key, start, end):
        return BidReport(
            kind=kind, periodKey=key, periodStart=start.replace(tzinfo=None), periodEnd=end.replace(tzinfo=None),
            bidderId=ana.id, ownerId=owner.id, bidderName="Ana", rate=0.5, count=0, amount=0.0, pending=1, rejected=0,
        )

    await row("daily", "d", daily_start, daily_end).insert()
    for key, end in (("w1", weekly), ("w2", weekly + timedelta(days=7))):
        await row("weekly", key, *window_for("weekly", end, sched)).insert()
    async with make_client(owner) as c:
        r = await c.post("/bids/review", json={"ids": [str(early.id), str(late.id)], "status": "approved"})
    assert r.json() == {"updated": 2, "skipped": 0}
    rows = {x.periodKey: (x.count, x.pending, x.amount) for x in await BidReport.find({"bidderId": ana.id}).to_list()}
    assert rows == {"d": (2, 0, 1.0), "w1": (1, 0, 0.5), "w2": (1, 0, 0.5)}
