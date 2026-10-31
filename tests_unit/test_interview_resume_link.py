"""Linking interviews to the resume and job they came from (in-memory MongoDB via mongomock-motor; S3 archive and
LLM scoring faked): company search, snapshots, and the create / edit rules."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")
os.environ["RESUME_QUEUE_DISABLED"] = "1"

from datetime import datetime

import pytest
from beanie import init_beanie
from fastapi import HTTPException
from mongomock_motor import AsyncMongoMockClient

from app import interview_resume_link as irl
from app import resume_retention
from app.config import get_settings
from app.job_apply import tailored_score
from app.job_apply.schema import TailoredMatch
from app.models import Account, Interview, InterviewQuestion, ResumeApplication, UserExtra
from app.models.job_apply import JobApplicationLog, JobInfo, JobPostingCache
from app.routers import interviews
from app.routers.interviews import InterviewCreate, InterviewUpdate, RoundIn, create_interview, update_interview

pytestmark = [  # pre-existing utcnow defaults (app/models/base.py) and Beanie internals
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]

SCORE = TailoredMatch(total=84, band="strong", missing=["Kafka"])


@pytest.fixture
async def db(monkeypatch):
    database = AsyncMongoMockClient()["interview-link-unit"]
    await init_beanie(database=database, document_models=[
        UserExtra, Account, Interview, InterviewQuestion, ResumeApplication, JobApplicationLog, JobInfo, JobPostingCache,
    ])
    scored: list = []

    async def fake_score(job, user=None):
        scored.append(job.plainText)
        return SCORE if job.plainText else None

    async def no_slack(*a, **kw):
        return None

    monkeypatch.setattr(tailored_score, "score_job", fake_score)
    monkeypatch.setattr(interviews, "_sync_caller_posts", no_slack)
    return scored


async def _owner_and_profiles():
    owner = await UserExtra(name="Athen", email="athen@x.io").insert()
    jordan = await Account(name="Jordan", createdBy=owner.id).insert()
    sam = await Account(name="Sam", createdBy=owner.id).insert()
    return owner, jordan, sam


async def _generated(owner, acc, company, when, **kw):
    fields = dict(
        userId=owner.id, accountId=acc.id, profileName=acc.name, companyName=company, companyNameLower=company.lower(),
        jobDescription="Python FastAPI role", status="completed", pdfFilename=f"out/{company}.pdf",
        plainText="resume text", createdAt=when,
    )
    fields.update(kw)
    return await ResumeApplication(**fields).insert()


async def _applied(owner, acc, company, when, **kw):
    fields = dict(
        userId=owner.id, url=f"https://jobs.example.com/{company}", company=company, accountId=str(acc.id),
        fileKey=f"r:{company}", resumeId="res1", filename="jordan_backend.pdf", appliedAt=when,
    )
    fields.update(kw)
    return await JobApplicationLog(**fields).insert()


def _round():
    return RoundIn(stage="intro", scheduledAt="2026-10-20T15:00:00Z")


# ---------- names and search ----------


def test_normalize_company_drops_case_punctuation_and_legal_forms():
    assert irl.normalize_company("Acme, Inc.") == "acme"
    assert irl.normalize_company("The Acme Corp") == "acme"
    assert irl.normalize_company("Bank of America") == "bank of america"
    assert irl.normalize_company("Inc") == "inc"  # nothing but a legal form: kept
    assert irl.normalize_company(None) == ""


async def test_search_finds_both_sources_for_the_profile_newest_first(db):
    owner, jordan, sam = await _owner_and_profiles()
    await _generated(owner, jordan, "Acme, Inc.", datetime(2026, 10, 1), matchScore=SCORE)
    await _applied(owner, jordan, "ACME Corp", datetime(2026, 10, 5), score={"total": 70, "band": "good"})
    await _generated(owner, jordan, "Acme Health", datetime(2026, 9, 1))
    await _generated(owner, jordan, "Globex", datetime(2026, 10, 2))
    await _generated(owner, sam, "Acme, Inc.", datetime(2026, 10, 3))  # another profile
    await _generated(owner, jordan, "Acme, Inc.", datetime(2026, 10, 4), status="failed")

    found = await irl.search_matches(jordan.id, "acme inc")

    assert [(m["kind"], m["company"]) for m in found] == [
        ("uploaded", "ACME Corp"), ("tailored", "Acme, Inc."), ("tailored", "Acme Health"),
    ]
    assert found[0]["score"]["total"] == 70 and found[1]["score"]["total"] == 84
    assert found[1]["filename"] == "Acme, Inc..pdf" and found[1]["appliedAt"] == "2026-10-01T00:00:00Z"
    assert await irl.search_matches(jordan.id, "a") == []  # too short


# ---------- snapshots ----------


async def test_tailored_snapshot_restores_archived_text_and_scores_once(db, monkeypatch):
    owner, jordan, _ = await _owner_and_profiles()
    old = await _generated(
        owner, jordan, "Acme", datetime(2026, 8, 1), jobDescription="", plainText=None,
        textArchiveKey="archive/k1", resumeLlmModel="gpt-5", promptName="Backend",
    )

    async def fake_archive(key, job_id):
        assert key == "archive/k1" and str(job_id) == str(old.id)
        return {"jobDescription": "Archived JD", "plainText": "archived resume"}

    monkeypatch.setattr(resume_retention, "archived_text", fake_archive)

    link = await irl.build_link(f"t:{old.id}", jordan.id)

    assert (link.kind, link.jobDescription, link.score) == ("tailored", "Archived JD", SCORE)
    assert (link.promptName, link.resumeModel, link.filename) == ("Backend", "gpt-5", "Acme.pdf")
    assert db == ["archived resume"]
    assert (await ResumeApplication.get(old.id)).matchScore == SCORE  # stored: not scored again
    await irl.build_link(f"t:{old.id}", jordan.id)
    assert db == ["archived resume"]


async def test_uploaded_snapshot_falls_back_to_the_jd_cache(db):
    owner, jordan, _ = await _owner_and_profiles()
    entry = await _applied(owner, jordan, "Acme", datetime(2026, 10, 5))
    await JobPostingCache(url=entry.url, text="Cached JD", source="fetch").insert()

    link = await irl.build_link(f"u:{entry.id}", jordan.id)

    assert (link.kind, link.resumeId, link.jobDescription, link.score) == ("uploaded", "res1", "Cached JD", None)


async def test_a_match_from_another_profile_is_refused(db):
    owner, jordan, sam = await _owner_and_profiles()
    theirs = await _generated(owner, sam, "Acme", datetime(2026, 10, 1))
    entry = await _applied(owner, sam, "Acme", datetime(2026, 10, 1))

    for match_id in (f"t:{theirs.id}", f"u:{entry.id}", "x:123", "t:not-an-id"):
        with pytest.raises(HTTPException) as err:
            await irl.build_link(match_id, jordan.id)
        assert err.value.status_code == 400


# ---------- create / edit ----------


async def test_create_links_the_picked_resume(db):
    owner, jordan, _ = await _owner_and_profiles()
    gen = await _generated(owner, jordan, "Acme", datetime(2026, 10, 1), matchScore=SCORE)

    out = await create_interview(InterviewCreate(
        accountId=str(jordan.id), companyName="Acme", round=_round(), resumeLink={"matchId": f"t:{gen.id}"},
    ), owner)

    saved = await Interview.get(out["_id"])
    assert saved.resumeLink.kind == "tailored" and saved.resumeLink.resumeJobId == gen.id
    assert saved.resumeLink.linkedBy == "user" and saved.resumeLink.score == SCORE
    assert out["resumeLink"]["jobDescription"] == "Python FastAPI role"


async def test_create_with_no_company_found_needs_a_reason(db):
    owner, jordan, _ = await _owner_and_profiles()

    def body(**link):
        return InterviewCreate(accountId=str(jordan.id), companyName="Initech", round=_round(), resumeLink=link)

    for bad in ({"none": True}, {"none": True, "reason": "other"}, {}):
        with pytest.raises(HTTPException) as err:
            await create_interview(body(**bad), owner)
        assert err.value.status_code == 400

    out = await create_interview(body(none=True, reason="recruiter"), owner)
    assert out["resumeLink"]["kind"] == "none" and out["resumeLink"]["reason"] == "recruiter"


async def test_create_without_a_link_follows_the_setting(db, monkeypatch):
    owner, jordan, _ = await _owner_and_profiles()
    body = InterviewCreate(accountId=str(jordan.id), companyName="Acme", round=_round())

    assert (await create_interview(body, owner))["resumeLink"] is None  # off: older forms still work

    monkeypatch.setattr(get_settings(), "INTERVIEW_RESUME_LINK_REQUIRED", True)
    with pytest.raises(HTTPException) as err:
        await create_interview(body, owner)
    assert err.value.status_code == 400


async def test_edit_keeps_relinks_and_drops_on_profile_change(db):
    owner, jordan, sam = await _owner_and_profiles()
    first = await _generated(owner, jordan, "Acme", datetime(2026, 10, 1))
    second = await _generated(owner, jordan, "Acme", datetime(2026, 10, 2))
    out = await create_interview(InterviewCreate(
        accountId=str(jordan.id), companyName="Acme", round=_round(), resumeLink={"matchId": f"t:{first.id}"},
    ), owner)
    iv_id = out["_id"]

    await update_interview(iv_id, InterviewUpdate(jobUrl="https://acme.com/job"), owner)
    assert (await Interview.get(iv_id)).resumeLink.resumeJobId == first.id  # kept

    await update_interview(iv_id, InterviewUpdate(resumeLink={"matchId": f"t:{second.id}"}), owner)
    assert (await Interview.get(iv_id)).resumeLink.resumeJobId == second.id

    await update_interview(iv_id, InterviewUpdate(accountId=str(sam.id)), owner)
    assert (await Interview.get(iv_id)).resumeLink is None  # it was Jordan's resume


async def test_lists_leave_out_the_job_description(db):
    owner, jordan, _ = await _owner_and_profiles()
    gen = await _generated(owner, jordan, "Acme", datetime(2026, 10, 1))
    await create_interview(InterviewCreate(
        accountId=str(jordan.id), companyName="Acme", round=_round(), resumeLink={"matchId": f"t:{gen.id}"},
    ), owner)

    listed = await interviews.list_interviews(owner, page=1, limit=10, from_="", to="", accountId="", stage="",
                                              status_="", creatorId="", callType="", sort="latest", dir="desc")

    link = listed["interviews"][0]["resumeLink"]
    assert link["kind"] == "tailored" and "jobDescription" not in link


async def test_search_and_preview_need_the_profile(db):
    owner, jordan, _ = await _owner_and_profiles()
    stranger = await UserExtra(name="Eve", email="eve@x.io").insert()
    gen = await _generated(owner, jordan, "Acme", datetime(2026, 10, 1))

    assert len((await interviews.resume_matches(owner, accountId=str(jordan.id), q="acme"))["matches"]) == 1
    preview = await interviews.resume_match(f"t:{gen.id}", owner, accountId=str(jordan.id))
    assert preview["jobDescription"] == "Python FastAPI role"
    with pytest.raises(HTTPException) as err:
        await interviews.resume_matches(stranger, accountId=str(jordan.id), q="acme")
    assert err.value.status_code == 403


# ---------- backfill ----------


async def test_backfill_links_the_latest_exact_match_before_the_first_round(db):
    from scripts.backfill_interview_resume_links import backfill

    owner, jordan, _ = await _owner_and_profiles()
    await _generated(owner, jordan, "Acme Inc", datetime(2026, 10, 1))
    newest = await _applied(owner, jordan, "ACME", datetime(2026, 10, 10))
    await _generated(owner, jordan, "Acme", datetime(2026, 10, 25))  # after the interview: not it
    await _generated(owner, jordan, "Acme Health", datetime(2026, 10, 15))  # not the same company
    linked = await create_interview(InterviewCreate(accountId=str(jordan.id), companyName="Acme, Inc.", round=_round()), owner)
    lonely = await create_interview(InterviewCreate(accountId=str(jordan.id), companyName="Initech", round=_round()), owner)

    dry = await backfill()
    assert dry["counts"] == {"seen": 2, "uploaded": 1, "unmatched": 1, "withJd": 0, "withScore": 0}
    assert (await Interview.get(linked["_id"])).resumeLink is None  # dry run writes nothing

    out = await backfill(apply=True)
    assert out["counts"]["written"] == 1
    link = (await Interview.get(linked["_id"])).resumeLink
    assert (link.kind, link.appliedLogId, link.linkedBy) == ("uploaded", newest.id, "auto")
    assert (await Interview.get(lonely["_id"])).resumeLink is None

    again = await backfill(apply=True)
    assert again["counts"] == {"seen": 1, "unmatched": 1}  # the linked one is skipped

# ---------- fixed-resume forks ----------


async def test_a_fixed_resume_fork_is_found_and_linked_as_fixed(db):
    owner, jordan, _ = await _owner_and_profiles()
    fork = await _generated(owner, jordan, "Initech", datetime(2026, 10, 1), source="job_applies",
                            fixedFrom="res1", pdfFilename="jordan_backend.pdf", matchScore=SCORE)

    found = await irl.search_matches(jordan.id, "initech")
    assert [(m["kind"], m["filename"]) for m in found] == [("fixed", "jordan_backend.pdf")]

    out = await create_interview(InterviewCreate(
        accountId=str(jordan.id), companyName="Initech", round=_round(), resumeLink={"matchId": f"t:{fork.id}"},
    ), owner)
    link = (await Interview.get(out["_id"])).resumeLink
    assert (link.kind, link.resumeJobId, link.score) == ("fixed", fork.id, SCORE)


async def test_the_used_a_fixed_resume_reason_is_no_longer_accepted(db):
    owner, jordan, _ = await _owner_and_profiles()
    with pytest.raises(HTTPException) as err:
        await create_interview(InterviewCreate(
            accountId=str(jordan.id), companyName="Initech", round=_round(),
            resumeLink={"none": True, "reason": "fixed_resume"},
        ), owner)
    assert err.value.status_code == 400

    # Links saved with it before still load.
    iv = await Interview.get((await create_interview(InterviewCreate(
        accountId=str(jordan.id), companyName="Initech", round=_round()), owner))["_id"])
    await Interview.get_motor_collection().update_one({"_id": iv.id}, {"$set": {"resumeLink": {
        "kind": "uploaded", "linkedBy": "user", "resumeId": "res1", "filename": "a.pdf", "reason": "fixed_resume"}}})
    assert (await Interview.get(iv.id)).resumeLink.reason == "fixed_resume"
