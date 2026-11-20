"""Applications sync against an in-memory MongoDB (mongomock-motor); no real database."""

import asyncio
from datetime import date, datetime, timedelta

import pytest
from beanie import init_beanie
from bson import ObjectId
from mongomock_motor import AsyncMongoMockClient

from app.applications import sync
from app.models import Account, Application, Bid, Interview, ResumeApplication
from app.models.bidder import Bidder

pytestmark = [
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]

ACC, OWN, BIDDER = ObjectId(), ObjectId(), ObjectId()
T0 = datetime(2026, 9, 1, 10)
SINCE = date(2026, 8, 1)
URL = "https://jobs.acme.com/1"


@pytest.fixture
async def db(monkeypatch):
    database = AsyncMongoMockClient()["apps-unit"]
    await init_beanie(database=database, document_models=[Application, Bid, ResumeApplication, Interview, Account, Bidder])

    async def loop_upsert(coll, ops):  # mongomock cannot bulk_write
        for k, u in ops:
            await coll.update_one({"key": k}, u, upsert=True)

    database.writes = []  # row counts per bulk write

    async def counting_upsert(coll, ops):
        database.writes.append(len(ops))
        await loop_upsert(coll, ops)

    monkeypatch.setattr(sync, "_bulk_upsert", counting_upsert)
    await database["accounts"].insert_one({"_id": ACC, "region": "EU", "createdAt": datetime(2026, 8, 1)})
    await database["bidders"].insert_one({"_id": BIDDER, "createdBy": OWN, "name": "B", "profileId": ACC, "rate": 1,
                                          "screenshotFolderUrl": "f"})
    return database


async def add_bid(db, url=URL, at=T0, **kw):
    """Production shape (app/bids/records.py): a bidder bid has no accountId; its profile is its Bidder's."""
    doc = {"_id": ObjectId(), "kind": "bidder", "bidderId": BIDDER, "ownerId": OWN,
           "jobKey": f"f/2026-09-01/p/{ObjectId()}", "jobUrl": url, "firstAt": at, "status": "pending",
           "createdAt": at, "updatedAt": at, "screenshots": [], **kw}
    await db["bids"].insert_one(doc)
    return doc


async def add_resume(db, url=URL, at=T0, **kw):
    doc = {"_id": ObjectId(), "userId": OWN, "accountId": ACC, "jobUrl": url, "createdAt": at, "updatedAt": at, **kw}
    await db["resumeapplications"].insert_one(doc)
    return doc


async def add_interview(db, url=URL, days=3, at=None, resume_id=None, acc=ACC):
    when = T0 + timedelta(days=days)
    doc = {"_id": ObjectId(), "accountId": acc, "createdBy": OWN, "scheduledAt": when, "stage": "intro",
           "status": "completed", "jobUrl": url, "stageHistory": [], "createdAt": at or when, "updatedAt": at or when}
    if resume_id:
        doc["resumeLink"] = {"kind": "tailored", "resumeJobId": resume_id}
    await db["interviews"].insert_one(doc)
    return doc


async def snapshot(db):
    return sorted(({k: v for k, v in d.items() if k not in ("_id", "syncedAt", "createdAt", "updatedAt")}
                   for d in await db["applications"].find({}).to_list(None)), key=lambda d: d["key"])


async def test_full_rebuild_idempotent(db):
    await add_bid(db)
    await add_bid(db, url="https://jobs.b.com/2")
    await add_interview(db)
    c1 = await sync.full_rebuild(SINCE, apply=True)
    s1 = await snapshot(db)
    c2 = await sync.full_rebuild(SINCE, apply=True)
    assert c1 == c2 == {"rows": 2, "matched": 1, "unmatched": 0, "noLink": 0, "deleted": 0}
    assert await snapshot(db) == s1 and len(s1) == 2
    assert await db["applications"].count_documents({}) == 2


async def test_dry_run_writes_nothing(db):
    await add_bid(db)
    c = await sync.full_rebuild(SINCE, apply=False)
    assert c["rows"] == 1 and await db["applications"].count_documents({}) == 0


async def test_deleted_bid_removed_on_full_rebuild(db):
    b = await add_bid(db)
    await sync.full_rebuild(SINCE, apply=True)
    await db["bids"].delete_one({"_id": b["_id"]})
    assert (await sync.full_rebuild(SINCE, apply=False))["deleted"] == 1
    assert await db["applications"].count_documents({}) == 1
    c = await sync.full_rebuild(SINCE, apply=True)
    assert c["deleted"] == 1 and await db["applications"].count_documents({}) == 0


async def test_incremental_adds_new_bid_and_matches_new_interview(db):
    await add_bid(db, at=T0 - timedelta(days=30))
    await sync.full_rebuild(SINCE, apply=True)
    cp = datetime(2026, 9, 10)
    await add_bid(db, url="https://jobs.b.com/2", at=cp + timedelta(hours=1))
    await add_interview(db, url="https://jobs.b.com/2", days=20, at=cp + timedelta(hours=2))
    await sync.incremental(cp)
    rows = {r["jobKey"]: r for r in await db["applications"].find({}).to_list(None)}
    assert set(rows) == {"jobs.acme.com/1", "jobs.b.com/2"}
    assert rows["jobs.b.com/2"].get("interviewId") and rows["jobs.acme.com/1"].get("interviewId") is None


async def test_relinked_interview_moves_match(db):
    r1 = await add_resume(db, url="https://a.com/1")
    r2 = await add_resume(db, url="https://b.com/2")
    iv = await add_interview(db, url=None, resume_id=r1["_id"])
    await sync.full_rebuild(SINCE, apply=True)
    old = await db["applications"].find_one({"jobKey": "a.com/1"})
    assert old["interviewId"] == iv["_id"]
    cp = datetime(2026, 9, 20)
    await db["interviews"].update_one({"_id": iv["_id"]}, {"$set": {
        "resumeLink.resumeJobId": r2["_id"], "updatedAt": cp + timedelta(hours=1)}})
    await sync.incremental(cp)
    old = await db["applications"].find_one({"jobKey": "a.com/1"})
    new = await db["applications"].find_one({"jobKey": "b.com/2"})
    assert new["interviewId"] == iv["_id"]
    for f in ("interviewId", "firstRoundAt", "daysToInterview", "furthest", "result"):
        assert f not in old


async def test_tick_runs_full_first_then_incremental(db):
    await add_bid(db)
    now = datetime(2026, 9, 20, 12)
    assert "rows" in await sync.run_applications_tick(now)  # full
    st = await db["app_settings"].find_one({"_id": "applications_sync"})
    assert st["checkpoint"] == now and st["lastFull"] == now
    await add_bid(db, url="https://jobs.b.com/2", at=now + timedelta(minutes=1), updatedAt=now + timedelta(minutes=1))
    later = now + timedelta(minutes=5)
    out = await sync.run_applications_tick(later)
    assert out.get("incremental") is True
    st = await db["app_settings"].find_one({"_id": "applications_sync"})
    assert st["checkpoint"] == later and st["lastFull"] == now
    assert await db["applications"].count_documents({}) == 2
    out = await sync.run_applications_tick(now + timedelta(hours=25))
    assert "incremental" not in out


async def test_tick_disabled_env(db, monkeypatch):
    monkeypatch.setenv("APPLICATIONS_SYNC_DISABLED", "1")
    assert await sync.run_applications_tick() == {"skipped": "disabled"}
    assert await db["app_settings"].count_documents({}) == 0


async def _cross_profile(db):
    acc_c = ObjectId()
    await db["accounts"].insert_one({"_id": acc_c, "region": "US", "createdAt": datetime(2026, 8, 1)})
    await add_bid(db)  # profile A's row for the same job
    rc = await add_resume(db)
    await db["resumeapplications"].update_one({"_id": rc["_id"]}, {"$set": {"accountId": acc_c}})
    await add_interview(db, resume_id=rc["_id"])  # on A, linked to C's resume
    await sync.full_rebuild(SINCE, apply=True)
    return acc_c


async def _holders(db):
    return {(r["accountId"]) for r in await db["applications"].find({"interviewId": {"$exists": True}}).to_list(None)}


@pytest.mark.parametrize("touch", ["A", "C"])
async def test_incremental_cross_profile_link_matches_once(db, touch):
    acc_c = await _cross_profile(db)
    assert await _holders(db) == {acc_c}
    cp = datetime(2026, 9, 20)
    coll, who = ("bids", {"bidderId": BIDDER}) if touch == "A" else ("resumeapplications", {"accountId": acc_c})
    await db[coll].update_many(who, {"$set": {"updatedAt": cp + timedelta(hours=1)}})
    await sync.incremental(cp)
    assert await _holders(db) == {acc_c}


async def test_second_incremental_writes_nothing(db):
    await add_bid(db)
    await add_interview(db)
    await sync.full_rebuild(SINCE, apply=True)
    db.writes.clear()
    await sync.incremental(datetime(2026, 8, 1))
    await sync.incremental(datetime(2026, 8, 1))
    assert db.writes == []


async def test_full_rebuild_timeout_keeps_last_full_and_retries_after_hour(db, monkeypatch):
    t0 = datetime(2026, 9, 20, 12)
    await add_bid(db)
    await sync.run_applications_tick(t0)
    real = sync.full_rebuild

    async def slow(*a, **k):
        await asyncio.sleep(5)

    monkeypatch.setattr(sync, "full_rebuild", slow)
    monkeypatch.setattr(sync, "FULL_TIMEOUT", 0.05)
    t1 = t0 + timedelta(hours=25)
    assert await sync.run_applications_tick(t1) == {"error": "timeout"}
    st = await db["app_settings"].find_one({"_id": "applications_sync"})
    assert st["lastFull"] == t0 and st["lastFullAttempt"] == t1 and st["checkpoint"] == t0
    assert (await sync.run_applications_tick(t1 + timedelta(minutes=10))).get("incremental") is True
    monkeypatch.setattr(sync, "full_rebuild", real)
    out = await sync.run_applications_tick(t1 + timedelta(minutes=70))
    assert "rows" in out and "incremental" not in out
    st = await db["app_settings"].find_one({"_id": "applications_sync"})
    assert st["lastFull"] == t1 + timedelta(minutes=70)


# ---- bidder bids: profile = Bid.accountId, else the Bidder's profileId ----

async def test_bidder_bid_and_tailored_resume_same_profile_one_row(db):
    b = await add_bid(db, at=T0 + timedelta(hours=1))
    r = await add_resume(db, url=URL + "?utm_source=li")
    iv = await add_interview(db)
    c = await sync.full_rebuild(SINCE, apply=True)
    assert c == {"rows": 1, "matched": 1, "unmatched": 0, "noLink": 0, "deleted": 0}
    row = await db["applications"].find_one({})
    assert row["key"] == f"{ACC}:jobs.acme.com/1" and row["accountId"] == ACC and row["region"] == "EU"
    assert row["by"] == "bidder" and row["bidderId"] == BIDDER and row["appliedAt"] == T0
    assert row["interviewId"] == iv["_id"]
    assert row["sources"] == [f"bid:{b['_id']}", f"resume:{r['_id']}"]


async def test_bidder_without_profile_keeps_source_key(db):
    lone = ObjectId()
    await db["bidders"].insert_one({"_id": lone, "createdBy": OWN, "name": "L", "rate": 1, "screenshotFolderUrl": "f"})
    b = await add_bid(db, bidderId=lone)
    await sync.full_rebuild(SINCE, apply=True)
    assert [d["key"] for d in await db["applications"].find({}).to_list(None)] == [f"src:bids:{b['_id']}"]


async def test_incremental_bidder_bid_rebuilds_only_its_profile(db, monkeypatch):
    acc2, lone = ObjectId(), ObjectId()
    await db["accounts"].insert_one({"_id": acc2, "region": "US", "createdAt": datetime(2026, 8, 1)})
    await db["bidders"].insert_one({"_id": lone, "createdBy": OWN, "name": "L", "rate": 1, "screenshotFolderUrl": "f"})
    await add_resume(db, url="https://other.com/1", accountId=acc2)
    await add_bid(db, url="https://jobs.lone.com/1", bidderId=lone)  # no profile: never in the touched set
    b = await add_bid(db)
    await add_resume(db)
    await sync.full_rebuild(SINCE, apply=True)
    cp = datetime(2026, 9, 20)
    await db["bids"].update_many({}, {"$set": {"updatedAt": cp + timedelta(hours=1)}})
    seen, real = [], sync._sync

    async def spy(since, account_ids, **kw):
        seen.append(account_ids)
        return await real(since, account_ids, **kw)

    monkeypatch.setattr(sync, "_sync", spy)
    out = await sync.incremental(cp)
    assert seen == [[ACC]] and out["rows"] == 1
    row = await db["applications"].find_one({"accountId": ACC})
    assert row["by"] == "bidder" and f"bid:{b['_id']}" in row["sources"]
    assert await db["applications"].count_documents({}) == 3


async def test_bid_job_title_used_for_bid_only_rows(db):
    await add_bid(db, jobKey="f/2026-09-01/p/acme_frontend-dev", jobTitle="Senior Backend Engineer")
    await sync.full_rebuild(SINCE, apply=True)
    row = await db["applications"].find_one({})
    assert row["title"] == "Senior Backend Engineer" and row["area"] == "Backend"


async def test_full_rebuild_failure_records_attempt_and_falls_back_to_incremental(db, monkeypatch, caplog):
    t0 = datetime(2026, 9, 20, 12)
    await add_bid(db)
    await sync.run_applications_tick(t0)

    async def boom(*a, **k):
        raise ValueError("bad interview doc")

    monkeypatch.setattr(sync, "full_rebuild", boom)
    t1 = t0 + timedelta(hours=25)
    assert await sync.run_applications_tick(t1) == {"error": "failed"}
    assert "stage=full_rebuild" in caplog.text and "bad interview doc" in caplog.text
    st = await db["app_settings"].find_one({"_id": "applications_sync"})
    assert st["lastFull"] == t0 and st["lastFullAttempt"] == t1 and st["checkpoint"] == t0
    assert (await sync.run_applications_tick(t1 + timedelta(minutes=5))).get("incremental") is True


async def test_backfill_script_apply_seeds_sync_state(db):
    from scripts.build_applications import build

    await add_bid(db)
    await build(SINCE, apply=False)
    assert await db["app_settings"].count_documents({}) == 0
    t = datetime(2026, 9, 20, 12)
    counts = await build(SINCE, apply=True, start=t)
    assert counts["rows"] == 1 and await db["applications"].count_documents({}) == 1
    st = await db["app_settings"].find_one({"_id": "applications_sync"})
    assert st["checkpoint"] == t and st["lastFull"] == t
    assert (await sync.run_applications_tick(t + timedelta(minutes=5))).get("incremental") is True


async def test_unapplied_job_applies_resume_counts_only_when_an_interview_links_it(db):
    """countsAsBid False = Job Applies tailoring never marked applied; an interview linked to it proves the apply."""
    linked = await add_resume(db, url="https://a.com/1", countsAsBid=False)
    await add_resume(db, url="https://b.com/2", countsAsBid=False)
    iv = await add_interview(db, url=None, resume_id=linked["_id"])
    c = await sync.full_rebuild(SINCE, apply=True)
    rows = await db["applications"].find({}).to_list(None)
    assert c["rows"] == 1 and c["matched"] == 1 and c["unmatched"] == 0
    assert rows[0]["jobKey"] == "a.com/1" and rows[0]["interviewId"] == iv["_id"]


async def test_interview_proven_unapplied_resume_row_flagged_by_interview_only(db):
    """C1: such a row exists only because it got an interview; flagged so stats can drop it from both sides."""
    linked = await add_resume(db, url="https://a.com/1", countsAsBid=False)
    await add_interview(db, url=None, resume_id=linked["_id"])
    await sync.full_rebuild(SINCE, apply=True)
    row = await db["applications"].find_one({})
    assert row["byInterviewOnly"] is True and "countsAsBid" not in row
    assert row["by"] == "unknown"


async def test_proven_unapplied_resume_with_bid_or_applied_resume_not_flagged(db):
    linked = await add_resume(db, url="https://a.com/1", countsAsBid=False)
    await add_bid(db, url="https://a.com/1")
    await add_interview(db, url=None, resume_id=linked["_id"])
    linked2 = await add_resume(db, url="https://b.com/2", countsAsBid=False)
    await add_resume(db, url="https://b.com/2", at=T0 + timedelta(hours=1))  # an applied resume, same job
    await add_interview(db, url=None, resume_id=linked2["_id"])
    await sync.full_rebuild(SINCE, apply=True)
    rows = await db["applications"].find({}).to_list(None)
    assert len(rows) == 2 and all("byInterviewOnly" not in r for r in rows)


async def test_by_interview_only_flag_unset_when_resume_later_marked_applied(db):
    linked = await add_resume(db, url="https://a.com/1", countsAsBid=False)
    await add_interview(db, url=None, resume_id=linked["_id"])
    await sync.full_rebuild(SINCE, apply=True)
    assert (await db["applications"].find_one({}))["byInterviewOnly"] is True
    await db["resumeapplications"].update_one({"_id": linked["_id"]}, {"$set": {"countsAsBid": True}})
    await sync.full_rebuild(SINCE, apply=True)
    assert "byInterviewOnly" not in await db["applications"].find_one({})


async def test_full_rebuild_enriches_and_unsets_when_tailored_removed(db):
    b = await add_bid(db, jobTitle="Backend Engineer")
    t = await add_resume(db, countsAsBid=False, matchScore={"total": 70, "band": "good"}, promptName="P",
                         jobDescription="Data Engineer\n" + "x" * 5000)
    c = await sync.full_rebuild(SINCE, apply=True)
    assert c["rows"] == 1
    row = await db["applications"].find_one({})
    assert row["sources"] == [f"bid:{b['_id']}"] and "byInterviewOnly" not in row
    assert (row["score"], row["scoreBand"], row["promptName"], row["title"], row["area"]) == (
        70, "good", "P", "Data Engineer", "Data")
    assert row["tailoredResumeId"] == str(t["_id"])
    await db["resumeapplications"].delete_one({"_id": t["_id"]})
    await sync.full_rebuild(SINCE, apply=True)
    row = await db["applications"].find_one({})
    for f in ("tailoredResumeId", "score", "scoreBand", "promptName"):
        assert f not in row
    assert (row["title"], row["area"]) == ("Backend Engineer", "Backend")


async def test_title_from_a_long_description_with_leading_blank_lines(db):
    """Only the first lines of jobDescription are fetched; the title is the first non-empty one."""
    await add_bid(db)
    await add_resume(db, jobDescription="\n  \nSenior Backend Engineer\n" + "details " * 5000)
    await sync.full_rebuild(SINCE, apply=True)
    [row] = await db["applications"].find({}).to_list(None)
    assert row["title"] == "Senior Backend Engineer" and row["area"] == "Backend"


async def test_general_software_title_reads_the_area_from_the_job_description(db):
    """Only resumes whose title names no speciality have more of the description fetched, to read the area from it."""
    await add_resume(db, url="https://x.io/1", jobDescription="Senior Software Engineer\nAcme\n"
                     "We build services in Python and Django with Postgres. You design REST APIs and microservices in Go.")
    await add_resume(db, url="https://x.io/2", jobDescription="Software Engineer\nWe value curiosity and kindness.")
    await sync.full_rebuild(SINCE, apply=True)
    rows = {r["title"]: r["area"] for r in await db["applications"].find({}).to_list(None)}
    assert rows == {"Senior Software Engineer": "Backend", "Software Engineer": "General software"}

    # a bid row titled by an unapplied tailored resume reads that resume's description
    await db["applications"].delete_many({}); await db["resumeapplications"].delete_many({})
    await add_bid(db)
    await add_resume(db, countsAsBid=False, jobDescription="Software Engineer\nYou run Kubernetes, Terraform, Docker "
                     "and Helm and own CI/CD and observability.")
    await sync.full_rebuild(SINCE, apply=True)
    [row] = await db["applications"].find({}).to_list(None)
    assert (row["title"], row["area"]) == ("Software Engineer", "DevOps")


async def test_interview_fit_score_reaches_the_row(db):
    await add_bid(db)
    iv = await add_interview(db)
    await db["interviews"].update_one({"_id": iv["_id"]}, {"$set": {"fitScore": {"total": 61, "band": "fair"}}})
    await sync.full_rebuild(SINCE, apply=True)
    row = await db["applications"].find_one({})
    assert row["interviewId"] == iv["_id"] and (row["score"], row["scoreBand"]) == (61, "fair")
