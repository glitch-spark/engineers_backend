"""Bid review routes (/bids) against an in-memory MongoDB (mongomock-motor); no real database."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from datetime import date, datetime, time, timedelta, timezone

import pytest
from beanie import PydanticObjectId, init_beanie
from mongomock_motor import AsyncMongoMockClient

from app.bids.storage import BidStorageError
from app.bids.windows import latest_cutoff, schedule_from_settings, window_for
from app.models import Account, ResumeApplication, UserExtra
from app.models.account import Resume
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
    await init_beanie(database=database, document_models=[Bidder, Bid, BidReport, Account, ResumeApplication, UserExtra, SlackAlertLog])
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
        "jobDomain": "jobs.example", "durationSec": 0, "profileName": None, "resumeNames": [],
        "tailoredResumeName": None, "rejectReason": None, "thumbUrl": None,  # nothing uploaded
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
        "isSubmit": False,
    }
    assert shots[1]["url"] == "https://signed/bid-screenshots/ana/2026-10-05/backend/one/step1_step_1.png"
    assert storage.signed == [("bid-screenshots", "ana/2026-10-05/backend/one/step1_step_1.png")]


async def test_screenshots_mark_submit(make_client, owner):
    start, _ = day_window()
    bid = await add_bid(await make_bidder(owner), "one", start + timedelta(hours=1))
    submit = Screenshot(key=bid.jobKey + "/step2_submit_2.png", step=3, trigger="submit",
                        capturedAt=(start + timedelta(hours=1, minutes=2)).replace(tzinfo=None), uploadedAt=start.replace(tzinfo=None))
    await Bid.get_motor_collection().update_one({"_id": bid.id}, {"$push": {"screenshots": submit.model_dump()}})
    async with make_client(owner) as c:
        shots = (await c.get(f"/bids/{bid.id}/screenshots")).json()
    assert [(s["step"], s["trigger"], s["isSubmit"]) for s in shots] == [(1, "step", False), (2, "step", False), (3, "submit", True)]


async def test_list_includes_evidence_and_timezone(make_client, owner):
    start, _ = day_window()
    profile = await Account(
        name="Ian Meron", createdBy=owner.id, resumes=[Resume(filename="A.pdf", markdown="x"), Resume(filename="B.pdf", markdown="x")]
    ).insert()
    ana = await make_bidder(owner)
    await Bidder.get_motor_collection().update_one({"_id": ana.id}, {"$set": {"profileId": profile.id}})
    bid = await add_bid(ana, "one", start + timedelta(hours=1), "pending")
    first, last = start + timedelta(hours=1), start + timedelta(hours=1, minutes=4, seconds=30)
    await Bid.get_motor_collection().update_one(
        {"_id": bid.id}, {"$set": {"screenshots.1.capturedAt": last.replace(tzinfo=None)}}
    )
    await ResumeApplication(
        userId=owner.id, accountId=profile.id, companyName="One", companyNameLower="one", jobDescription="jd",
        jobUrl="https://jobs.example/one", status="completed", pdfFilename="Ian_Meron_One.pdf", completedAt=first.replace(tzinfo=None),
    ).insert()
    async with make_client(owner) as c:
        body = (await c.get("/bids", params={"day": DAY})).json()
    assert body["timezone"] == "America/Chicago"
    (out,) = body["bids"]
    assert (out["jobDomain"], out["durationSec"], out["profileName"], out["resumeNames"], out["tailoredResumeName"]) == (
        "jobs.example", 270, "Ian Meron", ["A.pdf", "B.pdf"], "Ian_Meron_One.pdf")
    assert out["rejectReason"] is None


async def test_list_without_bids_or_profiles_still_answers(make_client, owner):
    async with make_client(owner) as c:
        body = (await c.get("/bids", params={"day": DAY})).json()
    assert body["bids"] == [] and body["timezone"] == "America/Chicago"


async def test_get_one_bid(make_client, owner):
    start, _ = day_window()
    ana = await make_bidder(owner)
    bid = await add_bid(ana, "one", start + timedelta(hours=1), "approved", reviewedByName="Pedro")
    async with make_client(owner) as c:
        r = await c.get(f"/bids/{bid.id}")
    assert r.status_code == 200, r.text
    out = r.json()
    assert (out["id"], out["bidderName"], out["status"], out["reviewedByName"]) == (str(bid.id), "Ana", "approved", "Pedro")
    assert (out["jobDomain"], out["durationSec"], out["profileName"], out["resumeNames"]) == ("jobs.example", 0, None, [])
    assert out["tailoredResumeName"] is None and out["rejectReason"] is None
    async with make_client(await make_user("Root", "admin")) as c:
        assert (await c.get(f"/bids/{bid.id}")).status_code == 200  # admins review every bidder
    async with make_client(await make_user("Other")) as c:
        foreign = await c.get(f"/bids/{bid.id}")
        missing = await c.get(f"/bids/{PydanticObjectId()}")
        bad_id = await c.get("/bids/not-an-id")
    assert [r.status_code for r in (foreign, missing, bad_id)] == [404] * 3
    assert foreign.json()["detail"] == "bid not found"


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


@pytest.mark.parametrize("now, day, start, end", [
    # Tue Oct 6 2026 is CDT (UTC-5); the default daily cutoff is 18:30 local.
    ("2026-10-06T15:00:00Z", "2026-10-06", "2026-10-05T23:30:00Z", "2026-10-06T23:30:00Z"),  # 10:00 Chicago
    ("2026-10-07T01:00:00Z", "2026-10-07", "2026-10-06T23:30:00Z", "2026-10-07T23:30:00Z"),  # 20:00 Chicago, already Oct 7 UTC
    ("2026-10-06T23:29:00Z", "2026-10-06", "2026-10-05T23:30:00Z", "2026-10-06T23:30:00Z"),  # 18:29, window still open
    ("2026-10-06T23:30:00Z", "2026-10-07", "2026-10-06T23:30:00Z", "2026-10-07T23:30:00Z"),  # 18:30 sharp opens the next one
])
async def test_default_day_is_the_open_window(make_client, owner, monkeypatch, now, day, start, end):
    from app.routers import bids

    monkeypatch.setattr(bids, "_now", lambda: datetime.fromisoformat(now.replace("Z", "+00:00")))
    async with make_client(owner) as c:
        body = (await c.get("/bids")).json()
    assert (body["day"], body["start"], body["end"]) == (day, start, end)


async def test_open_window_default_spans_a_dst_change(make_client, owner, monkeypatch):
    from app.routers import bids

    # Chicago falls back on Sun Nov 1 2026 at 02:00, so the window ending Nov 1 18:30 CST is 25 hours long.
    monkeypatch.setattr(bids, "_now", lambda: datetime(2026, 11, 1, 1, 0, tzinfo=timezone.utc))  # Sat Oct 31 20:00 CDT
    async with make_client(owner) as c:
        body = (await c.get("/bids")).json()
    assert (body["day"], body["start"], body["end"]) == ("2026-11-01", "2026-10-31T23:30:00Z", "2026-11-02T00:30:00Z")


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


async def complete(bid: Bid) -> Bid:
    """Make a bid complete: Submit captured and every screenshot confirmed uploaded."""
    at = bid.firstAt
    await Bid.get_motor_collection().update_one(
        {"_id": bid.id},
        {"$set": {"submittedAt": at, "screenshots.0.uploadedAt": at, "screenshots.1.uploadedAt": at}},
    )
    return await Bid.get(bid.id)


async def test_reject_with_reason_is_stored_and_returned(make_client, owner):
    start, _ = day_window()
    bid = await add_bid(await make_bidder(owner), "one", start + timedelta(hours=1))
    async with make_client(owner) as c:
        r = await c.put(f"/bids/{bid.id}/review", json={"status": "rejected", "reason": "wrong_profile", "note": " not mine "})
        listed = (await c.get("/bids", params={"day": DAY})).json()["bids"]
        one = (await c.get(f"/bids/{bid.id}")).json()
    assert r.status_code == 200, r.text
    assert (r.json()["status"], r.json()["rejectReason"], r.json()["note"]) == ("rejected", "wrong_profile", "not mine")
    assert r.json()["jobDomain"] == "jobs.example"  # still the evidence-bearing BidOut
    assert (await Bid.get(bid.id)).rejectReason == "wrong_profile"
    assert [b["rejectReason"] for b in listed] == ["wrong_profile"] and one["rejectReason"] == "wrong_profile"


@pytest.mark.parametrize("reason", ["no_submission", "wrong_profile", "not_job_page", "duplicate", "incomplete"])
async def test_reasons_other_than_other_do_not_need_a_note(make_client, owner, reason):
    start, _ = day_window()
    bid = await add_bid(await make_bidder(owner), "one", start + timedelta(hours=1))
    async with make_client(owner) as c:
        r = await c.put(f"/bids/{bid.id}/review", json={"status": "rejected", "reason": reason})
    assert r.status_code == 200, r.text
    assert (r.json()["rejectReason"], r.json()["note"]) == (reason, None)


async def test_reject_without_a_reason_is_allowed_and_clears_the_old_one(make_client, owner):
    start, _ = day_window()
    bid = await add_bid(await make_bidder(owner), "one", start + timedelta(hours=1), "rejected", rejectReason="duplicate")
    assert (await Bid.get(bid.id)).rejectReason == "duplicate"  # the field is stored at all
    async with make_client(owner) as c:
        r = await c.put(f"/bids/{bid.id}/review", json={"status": "rejected"})
    assert (r.status_code, r.json()["rejectReason"]) == (200, None)


async def test_other_needs_a_note(make_client, owner):
    start, _ = day_window()
    bid = await add_bid(await make_bidder(owner), "one", start + timedelta(hours=1))
    async with make_client(owner) as c:
        missing = await c.put(f"/bids/{bid.id}/review", json={"status": "rejected", "reason": "other"})
        blank = await c.put(f"/bids/{bid.id}/review", json={"status": "rejected", "reason": "other", "note": "   "})
        empty = await c.put(f"/bids/{bid.id}/review", json={"status": "rejected", "reason": "other", "note": ""})
        assert [r.status_code for r in (missing, blank, empty)] == [400] * 3
        assert {r.json()["detail"] for r in (missing, blank, empty)} == {"A note is required for Other"}
        stored = await Bid.get(bid.id)
        assert (stored.status, stored.rejectReason, stored.reviewedBy) == ("pending", None, None)
        ok = await c.put(f"/bids/{bid.id}/review", json={"status": "rejected", "reason": "other", "note": "spam site"})
    assert (ok.status_code, ok.json()["rejectReason"], ok.json()["note"]) == (200, "other", "spam site")


async def test_approve_clears_reject_reason(make_client, owner):
    start, _ = day_window()
    bid = await add_bid(await make_bidder(owner), "one", start + timedelta(hours=1), "rejected", rejectReason="duplicate")
    assert (await Bid.get(bid.id)).rejectReason == "duplicate"  # the field is stored at all
    async with make_client(owner) as c:
        r = await c.put(f"/bids/{bid.id}/review", json={"status": "approved"})
    assert (r.status_code, r.json()["status"], r.json()["rejectReason"]) == (200, "approved", None)
    assert (await Bid.get(bid.id)).rejectReason is None


async def test_pending_clears_reject_reason(make_client, owner):
    start, _ = day_window()
    bid = await add_bid(await make_bidder(owner), "one", start + timedelta(hours=1), "rejected", rejectReason="incomplete")
    assert (await Bid.get(bid.id)).rejectReason == "incomplete"  # the field is stored at all
    async with make_client(owner) as c:
        r = await c.put(f"/bids/{bid.id}/review", json={"status": "pending"})
    assert (r.status_code, r.json()["status"], r.json()["rejectReason"]) == (200, "pending", None)
    assert (await Bid.get(bid.id)).rejectReason is None


async def test_reason_without_rejection_is_400(make_client, owner):
    start, _ = day_window()
    ana = await make_bidder(owner)
    bid = await add_bid(ana, "one", start + timedelta(hours=1))
    async with make_client(owner) as c:
        responses = [
            await c.put(f"/bids/{bid.id}/review", json={"status": status, "reason": "duplicate"})
            for status in ("approved", "pending")
        ]
        responses.append(await c.post("/bids/review", json={"ids": [str(bid.id)], "status": "approved", "reason": "duplicate"}))
        unknown = await c.put(f"/bids/{bid.id}/review", json={"status": "rejected", "reason": "because"})
    assert [r.status_code for r in responses] == [400] * 3
    assert {r.json()["detail"] for r in responses} == {"A reason only goes with a rejection"}
    assert unknown.status_code == 422  # not one of the reasons; the real app maps validation errors to 400
    stored = await Bid.get(bid.id)
    assert (stored.status, stored.rejectReason) == ("pending", None)


async def test_bulk_reject_stores_the_reason_and_other_needs_a_note(make_client, owner):
    start, _ = day_window()
    ana = await make_bidder(owner)
    bids = [await add_bid(ana, job, start + timedelta(hours=h)) for h, job in [(1, "a"), (2, "b")]]
    ids = [str(b.id) for b in bids]
    async with make_client(owner) as c:
        other = await c.post("/bids/review", json={"ids": ids, "status": "rejected", "reason": "other"})  # bulk has no note
        ok = await c.post("/bids/review", json={"ids": ids, "status": "rejected", "reason": "duplicate"})
        approved = await c.post("/bids/review", json={"ids": ids[:1], "status": "approved"})
    assert (other.status_code, other.json()["detail"]) == (400, "A note is required for Other")
    assert ok.json() == {"updated": 2, "skipped": 0} and approved.json() == {"updated": 1, "skipped": 0}
    assert [b.rejectReason for b in await Bid.find({"bidderId": ana.id}).sort("firstAt").to_list()] == [None, "duplicate"]


async def test_bulk_only_complete_skips_rejected_incomplete_and_unconfirmed(make_client, owner):
    start, _ = day_window()
    ana = await make_bidder(owner)
    good = await complete(await add_bid(ana, "good", start + timedelta(hours=1)))
    rejected = await complete(await add_bid(ana, "rejected", start + timedelta(hours=2), "rejected", rejectReason="duplicate"))
    no_submit = await add_bid(ana, "nosubmit", start + timedelta(hours=3))
    for shot in (0, 1):  # every screenshot confirmed, but no Submit
        await Bid.get_motor_collection().update_one({"_id": no_submit.id}, {"$set": {f"screenshots.{shot}.uploadedAt": no_submit.firstAt}})
    unconfirmed = await add_bid(ana, "unconfirmed", start + timedelta(hours=4))  # one screenshot never confirmed
    await Bid.get_motor_collection().update_one({"_id": unconfirmed.id}, {"$set": {"submittedAt": unconfirmed.firstAt}})
    await add_report(ana, owner, pending=3, rejected=1)
    ids = [str(b.id) for b in (good, rejected, no_submit, unconfirmed)]
    async with make_client(owner) as c:
        r = await c.post("/bids/review", json={"ids": ids, "status": "approved", "onlyComplete": True})
    assert r.status_code == 200, r.text
    assert r.json() == {"updated": 1, "skipped": 3}
    statuses = {b.jobTitle: (b.status, b.rejectReason) for b in await Bid.find({"bidderId": ana.id}).to_list()}
    assert statuses == {"Good": ("approved", None), "Rejected": ("rejected", "duplicate"), "Nosubmit": ("pending", None),
                        "Unconfirmed": ("pending", None)}
    assert (await Bid.get(good.id)).reviewedByName == "Pedro"
    assert (await Bid.get(rejected.id)).reviewedByName is None  # untouched
    row = await BidReport.find_one(BidReport.bidderId == ana.id)
    assert (row.count, row.pending, row.rejected) == (1, 2, 1)


async def test_bulk_only_complete_rechecks_completeness_in_the_write(make_client, owner, monkeypatch):
    """A bid that stops being complete between the narrowing read and the write is not approved, and counts as skipped."""
    start, _ = day_window()
    ana = await make_bidder(owner)
    stays = await complete(await add_bid(ana, "stays", start + timedelta(hours=1)))
    rejected_meanwhile = await complete(await add_bid(ana, "rejected-meanwhile", start + timedelta(hours=2)))
    unsubmitted_meanwhile = await complete(await add_bid(ana, "unsubmitted-meanwhile", start + timedelta(hours=3)))
    await add_report(ana, owner, pending=3)
    real = Bid.get_motor_collection()
    raced = []

    class RacingCollection:
        """The real collection, except two bids change right before the bulk write (after the route read them as complete)."""

        def __getattr__(self, name):
            return getattr(real, name)

        async def update_many(self, *args, **kwargs):
            await real.update_one({"_id": rejected_meanwhile.id}, {"$set": {"status": "rejected", "rejectReason": "duplicate"}})
            await real.update_one({"_id": unsubmitted_meanwhile.id}, {"$set": {"submittedAt": None}})
            raced.append(True)
            return await real.update_many(*args, **kwargs)

    monkeypatch.setattr(Bid, "get_motor_collection", classmethod(lambda cls: RacingCollection()))
    ids = [str(b.id) for b in (stays, rejected_meanwhile, unsubmitted_meanwhile)]
    async with make_client(owner) as c:
        r = await c.post("/bids/review", json={"ids": ids, "status": "approved", "onlyComplete": True})
    assert r.status_code == 200, r.text
    assert raced == [True]  # the change really landed between the read and the write
    assert r.json() == {"updated": 1, "skipped": 2}
    monkeypatch.undo()
    assert (await Bid.get(stays.id)).status == "approved"
    gone = await Bid.get(rejected_meanwhile.id)
    assert (gone.status, gone.rejectReason, gone.reviewedByName) == ("rejected", "duplicate", None)  # not approved over
    unsubmitted = await Bid.get(unsubmitted_meanwhile.id)
    assert (unsubmitted.status, unsubmitted.reviewedByName) == ("pending", None)
    row = await BidReport.find_one(BidReport.bidderId == ana.id)
    assert (row.count, row.pending, row.rejected) == (1, 1, 1)


async def test_bulk_only_complete_still_skips_foreign_ids_and_refreshes_once_per_period(make_client, owner, monkeypatch):
    from app.routers import bids

    start, _ = day_window()
    ana = await make_bidder(owner)
    stranger = await make_bidder(await make_user("Other"), "Zed")
    mine = [await complete(await add_bid(ana, job, start + timedelta(hours=h))) for h, job in [(1, "a"), (2, "b")]]
    foreign = await complete(await add_bid(stranger, "z", start + timedelta(hours=1)))
    refreshed = []

    async def spy(bid):
        refreshed.append(bid.bidderId)

    monkeypatch.setattr(bids, "refresh_report_rows", spy)
    async with make_client(owner) as c:
        r = await c.post("/bids/review", json={"ids": [str(b.id) for b in (*mine, foreign)], "status": "approved", "onlyComplete": True})
    assert r.json() == {"updated": 2, "skipped": 1}
    assert refreshed == [ana.id]
    assert (await Bid.get(foreign.id)).status == "pending"


async def test_only_complete_only_goes_with_approved(make_client, owner):
    start, _ = day_window()
    bid = await complete(await add_bid(await make_bidder(owner), "one", start + timedelta(hours=1)))
    async with make_client(owner) as c:
        responses = [
            await c.post("/bids/review", json={"ids": [str(bid.id)], "status": status, "onlyComplete": True})
            for status in ("rejected", "pending")
        ]
    assert [r.status_code for r in responses] == [400, 400]
    assert {r.json()["detail"] for r in responses} == {"onlyComplete only goes with approved"}
    assert (await Bid.get(bid.id)).status == "pending"


async def test_bulk_without_only_complete_unchanged(make_client, owner):
    start, _ = day_window()
    ana = await make_bidder(owner)
    incomplete = await add_bid(ana, "incomplete", start + timedelta(hours=1))  # no Submit, unconfirmed screenshots
    rejected = await add_bid(ana, "rejected", start + timedelta(hours=2), "rejected", rejectReason="duplicate")
    ids = [str(incomplete.id), str(rejected.id)]
    async with make_client(owner) as c:
        explicit_false = await c.post("/bids/review", json={"ids": ids[:1], "status": "approved", "onlyComplete": False})
        default = await c.post("/bids/review", json={"ids": ids[1:], "status": "approved"})
    assert explicit_false.json() == {"updated": 1, "skipped": 0} and default.json() == {"updated": 1, "skipped": 0}
    assert [(b.status, b.rejectReason) for b in await Bid.find({"bidderId": ana.id}).sort("firstAt").to_list()] == [
        ("approved", None)] * 2


async def test_list_thumb_is_the_submit_screenshot_else_the_last_uploaded_step(make_client, owner):
    start, _ = day_window()
    bidder = await make_bidder(owner)
    plain = await add_bid(bidder, "plain", start + timedelta(hours=1))
    submitted = await add_bid(bidder, "submitted", start + timedelta(hours=2))
    naive = (start + timedelta(hours=2)).astimezone(timezone.utc).replace(tzinfo=None)
    submitted.screenshots.append(Screenshot(key=f"{submitted.jobKey}/step2_submit.png", step=3, trigger="submit",
                                            capturedAt=naive, uploadedAt=naive))
    await submitted.save()
    none = await add_bid(bidder, "none", start + timedelta(hours=3), uploaded=False)
    async with make_client(owner) as c:
        r = await c.get("/bids", params={"day": DAY})
    assert r.status_code == 200, r.text
    thumbs = {b["id"]: b["thumbUrl"] for b in r.json()["bids"]}
    assert thumbs == {
        str(plain.id): "https://signed/bid-screenshots/ana/2026-10-05/backend/plain/step1_step_1.png",
        str(submitted.id): "https://signed/bid-screenshots/ana/2026-10-05/backend/submitted/step2_submit.png",
        str(none.id): None,
    }


async def test_list_thumb_is_null_when_signing_fails(make_client, owner, storage, monkeypatch):
    start, _ = day_window()
    await add_bid(await make_bidder(owner), "one", start + timedelta(hours=1))

    def broken(bucket, key):
        raise BidStorageError("bad endpoint")

    monkeypatch.setattr(storage, "presign_get", broken)
    async with make_client(owner) as c:
        r = await c.get("/bids", params={"day": DAY})
    assert r.status_code == 200, r.text
    assert [b["thumbUrl"] for b in r.json()["bids"]] == [None]
