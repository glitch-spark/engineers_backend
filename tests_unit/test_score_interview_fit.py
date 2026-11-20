"""One-off fit score for interviews without a scored resume, against an in-memory MongoDB; score_manual patched."""

import asyncio
from datetime import date, datetime

import pytest
from beanie import init_beanie
from bson import ObjectId
from mongomock_motor import AsyncMongoMockClient

from app.job_apply import tailored_score
from app.job_apply.schema import TailoredMatch
from app.models import Account, Interview, ResumeApplication
from app.models.user_extra import UserExtra
from scripts import score_interview_fit as sif

pytestmark = [
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]

ACC, OWN, OLD_RES, NEW_RES, UP_RES = ObjectId(), ObjectId(), ObjectId(), ObjectId(), ObjectId()
WHEN = datetime(2026, 10, 20, 15)
SINCE = date(2026, 10, 6)


@pytest.fixture
async def db(monkeypatch):
    database = AsyncMongoMockClient()["fit-unit"]
    await init_beanie(database=database, document_models=[Interview, Account, ResumeApplication, UserExtra])
    await database["accounts"].insert_one({"_id": ACC, "name": "P", "createdBy": OWN, "resumes": [
        {"id": OLD_RES, "filename": "old.pdf", "markdown": "old base", "uploadedAt": datetime(2026, 1, 1)},
        {"id": NEW_RES, "filename": "new.pdf", "markdown": "new base", "uploadedAt": datetime(2026, 9, 1)},
        {"id": UP_RES, "filename": "up.pdf", "markdown": "uploaded one", "uploadedAt": datetime(2025, 1, 1)},
    ]})
    await database["userextras"].insert_one({"_id": OWN, "name": "Owner"})
    calls = []

    async def fake_score(jd, text, user):
        calls.append((jd, text, getattr(user, "id", None)))
        return None if "junk" in jd else TailoredMatch(total=72, band="good")

    monkeypatch.setattr(tailored_score, "score_manual", fake_score)
    database.calls = calls
    return database


async def add_iv(db, link, at=WHEN, acc=ACC, **kw):
    doc = {"_id": ObjectId(), "accountId": acc, "createdBy": OWN, "scheduledAt": at, "stage": "intro",
           "status": "completed", "stageHistory": [], "createdAt": at, "updatedAt": at, **kw}
    if link is not None:
        doc["resumeLink"] = link
    await db["interviews"].insert_one(doc)
    return doc["_id"]


async def fit(db, _id):
    return (await db["interviews"].find_one({"_id": _id})).get("fitScore")


async def test_candidates_skip_tailored_no_jd_scored_and_old(db):
    await add_iv(db, {"kind": "none", "jobDescription": "Backend engineer"})
    await add_iv(db, {"kind": "uploaded", "resumeId": str(UP_RES), "jobDescription": "Backend engineer"})
    await add_iv(db, {"kind": "tailored", "resumeJobId": ObjectId(), "jobDescription": "Backend engineer"})
    await add_iv(db, {"kind": "none", "jobDescription": "  "})
    await add_iv(db, {"kind": "none"})
    await add_iv(db, None)
    await add_iv(db, {"kind": "none", "jobDescription": "x"}, fitScore={"total": 50, "band": "fair"})
    await add_iv(db, {"kind": "none", "jobDescription": "x"}, at=datetime(2026, 9, 1))
    out = await sif.score_interview_fit(False, SINCE)
    assert out == {"candidates": 2, "byKind": {"none": 1, "uploaded": 1}, "scored": 0, "noResume": 0, "unscorable": 0}
    assert db.calls == []  # dry run: no scoring, no LLM cost
    assert await db["interviews"].count_documents({"fitScore": {"$exists": True}}) == 1


async def test_apply_stores_fit_score_from_the_right_resume_and_is_idempotent(db):
    fork = ObjectId()
    await db["resumeapplications"].insert_one({"_id": fork, "accountId": ACC, "plainText": "fork text"})
    none_id = await add_iv(db, {"kind": "none", "jobDescription": "JD none"})
    up_id = await add_iv(db, {"kind": "uploaded", "resumeId": str(UP_RES), "jobDescription": "JD up"})
    fixed_id = await add_iv(db, {"kind": "fixed", "resumeJobId": fork, "jobDescription": "JD fixed"})
    junk_id = await add_iv(db, {"kind": "none", "jobDescription": "junk page"})
    out = await sif.score_interview_fit(True, SINCE)
    assert out == {"candidates": 4, "byKind": {"none": 2, "uploaded": 1, "fixed": 1}, "scored": 3, "noResume": 0,
                   "unscorable": 1}
    assert sorted(c[:2] for c in db.calls) == [("JD fixed", "fork text"), ("JD none", "new base"),
                                               ("JD up", "uploaded one"), ("junk page", "new base")]
    assert {c[2] for c in db.calls} == {OWN}  # scored for the profile's owner
    for i in (none_id, up_id, fixed_id):
        assert await fit(db, i) == {"total": 72, "band": "good"}
    assert await fit(db, junk_id) is None
    db.calls.clear()
    again = await sif.score_interview_fit(True, SINCE)
    assert again["candidates"] == 1 and again["scored"] == 0 and db.calls == [("junk page", "new base", OWN)]


async def test_no_base_resume_counts_no_resume(db):
    bare = ObjectId()
    await db["accounts"].insert_one({"_id": bare, "name": "Bare", "createdBy": OWN, "resumes": []})
    _id = await add_iv(db, {"kind": "none", "jobDescription": "JD"}, acc=bare)
    out = await sif.score_interview_fit(True, SINCE)
    assert out["noResume"] == 1 and out["scored"] == 0 and db.calls == [] and await fit(db, _id) is None


async def test_scoring_timeout_or_error_is_unscorable(db, monkeypatch):
    async def slow(jd, text, user):
        if "boom" in jd:
            raise RuntimeError("llm down")
        await asyncio.sleep(1)

    monkeypatch.setattr(tailored_score, "score_manual", slow)
    monkeypatch.setattr(sif, "CALL_TIMEOUT", 0.01)
    await add_iv(db, {"kind": "none", "jobDescription": "slow"})
    await add_iv(db, {"kind": "none", "jobDescription": "boom"})
    out = await sif.score_interview_fit(True, SINCE)
    assert out["unscorable"] == 2 and out["scored"] == 0
