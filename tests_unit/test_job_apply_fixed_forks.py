"""Fixed-resume forks: the uploaded resume scoring picks for a job becomes the job's own copy, like a tailored resume
(in-memory MongoDB via mongomock-motor; the S3 copy is faked)."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")
os.environ["RESUME_QUEUE_DISABLED"] = "1"

from datetime import datetime

import pytest
from beanie import init_beanie
from mongomock_motor import AsyncMongoMockClient

from app import resume_files, resume_queue
from app.job_apply import fixed_forks
from app.job_apply.schema import ResumeScore, TermHit
from app.models import Account, Interview, InterviewQuestion, ResumeApplication, UserExtra
from app.models.account import Resume
from app.models.job_apply import JobApplicationLog, JobApplyRow, JobApplyRun, JobPostingCache, ProfileGates
from app.routers.job_applies import AppliedBody, RunPatch, set_row_applied, update_run

pytestmark = [  # pre-existing utcnow defaults (app/models/base.py) and Beanie internals
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]

JD = "Senior Python role at Acme with Kafka"


@pytest.fixture
async def s3(monkeypatch):
    database = AsyncMongoMockClient()["fixed-forks-unit"]
    await init_beanie(database=database, document_models=[
        UserExtra, Account, JobApplyRun, JobApplyRow, JobApplicationLog, JobPostingCache, ResumeApplication,
        Interview, InterviewQuestion,
    ])
    calls = {"copied": [], "deleted": []}

    async def fake_copy(src, dst):
        calls["copied"].append((src, dst))
        return True

    async def fake_delete(keys):
        calls["deleted"].extend(keys)

    monkeypatch.setattr(fixed_forks, "copy_file", fake_copy)
    monkeypatch.setattr(resume_files, "delete_resume_files", fake_delete)
    return calls


def _score(acc, resume, total) -> ResumeScore:
    return ResumeScore(
        accountId=str(acc.id), resumeId=str(resume.id), filename=resume.filename, total=total, band="good",
        terms=[TermHit(term="Kafka", tier="required", weight=2.0, credit=0.0, match="missing", where="none")],
    )


async def _setup(threshold=75, total=82):
    owner = await UserExtra(name="Athen", email="athen@x.io").insert()
    backend = Resume(filename="alex_backend.pdf", markdown="Alex, Python", s3Key="profiles/a/resumes/1.pdf",
                     fileType="application/pdf")
    frontend = Resume(filename="alex_frontend.pdf", markdown="Alex, React", s3Key="profiles/a/resumes/2.pdf",
                      fileType="application/pdf")
    notes = Resume(filename="alex.md", markdown="Alex, Go", s3Key="profiles/a/resumes/3.md", fileType="text/markdown")
    alex = await Account(name="Alex", createdBy=owner.id, resumes=[backend, frontend, notes]).insert()
    run = await JobApplyRun(userId=owner.id, fileName="jobs.csv", status="done", threshold=threshold).insert()
    row = await JobApplyRow(
        runId=run.id, userId=owner.id, rowIndex=0, url="https://jobs.example.com/a", company="Acme", status="scored",
        jdText=JD, scoredAt=datetime(2026, 10, 24), bestByProfile=[_score(alex, backend, total)],
        scores=[_score(alex, backend, total)], profileGates=[ProfileGates(accountId=str(alex.id))],
    ).insert()
    return owner, alex, (backend, frontend, notes), run, row


async def _slot(row, acc):
    fresh = await JobApplyRow.get(row.id)
    return fresh.tailored.get(str(acc.id)), fresh


async def test_the_picked_resume_becomes_the_jobs_own_copy(s3):
    owner, alex, (backend, _, _), run, row = await _setup()

    assert await fixed_forks.sync_row(row, run.threshold) == 1

    fork_id, _ = await _slot(row, alex)
    fork = await ResumeApplication.get(fork_id)
    assert (fork.fixedFrom, fork.pdfFilename, fork.status, fork.source) == (
        str(backend.id), "alex_backend.pdf", "completed", "job_applies")
    assert (fork.companyName, fork.jobDescription, fork.jobUrl, fork.jobApplyRowId) == ("Acme", JD, row.url, row.id)
    assert fork.matchScore.total == 82 and fork.matchScore.missing == ["Kafka"]
    assert fork.countsAsBid is False and fork.plainText == "Alex, Python"
    assert s3["copied"] == [(backend.s3Key, fork.s3Key)] and fork.s3Key.startswith(f"job-applies/fixed/{alex.id}/")
    assert await resume_queue.daily_count(owner) == 0  # not a generation: the daily cap ignores it


async def test_same_pick_is_refreshed_and_another_pick_replaces_it(s3):
    owner, alex, (backend, frontend, _), run, row = await _setup()
    await fixed_forks.sync_row(row, run.threshold)
    first, _ = await _slot(row, alex)

    row.bestByProfile = [_score(alex, backend, 90)]
    assert await fixed_forks.sync_row(row, run.threshold) == 0
    assert (await _slot(row, alex))[0] == first and (await ResumeApplication.get(first)).matchScore.total == 90

    row.bestByProfile = [_score(alex, frontend, 88)]
    assert await fixed_forks.sync_row(row, run.threshold) == 1
    second, _ = await _slot(row, alex)
    assert second != first and (await ResumeApplication.get(second)).fixedFrom == str(frontend.id)
    assert await ResumeApplication.get(first) is None  # unused: deleted with its file
    assert len(s3["deleted"]) == 1


async def test_a_raised_minimum_retires_the_fork_unless_it_is_used(s3):
    owner, alex, _, run, row = await _setup(total=80)
    await fixed_forks.sync_row(row, run.threshold)
    fork_id, _ = await _slot(row, alex)

    await update_run(str(run.id), RunPatch(threshold=85), owner)  # 80 is no longer enough

    slot, _ = await _slot(row, alex)
    assert slot is None and await ResumeApplication.get(fork_id) is None

    await update_run(str(run.id), RunPatch(threshold=75), owner)
    fork_id, fresh = await _slot(row, alex)
    await set_row_applied(str(row.id), AppliedBody(accountId=str(alex.id), tailoredJobId=str(fork_id), applied=True), owner)
    await update_run(str(run.id), RunPatch(threshold=85), owner)
    assert (await _slot(row, alex))[0] == fork_id  # applied with: kept


async def test_marking_applied_with_a_fork_makes_it_a_bid(s3):
    owner, alex, _, run, row = await _setup()
    await fixed_forks.sync_row(row, run.threshold)
    fork_id, _ = await _slot(row, alex)

    await set_row_applied(str(row.id), AppliedBody(accountId=str(alex.id), tailoredJobId=str(fork_id), applied=True), owner)

    assert (await ResumeApplication.get(fork_id)).countsAsBid is True
    entry = await JobApplicationLog.find_one(JobApplicationLog.rowId == row.id)
    assert entry.tailoredJobId == str(fork_id) and entry.filename == "alex_backend.pdf"


async def test_a_tailored_resume_is_never_touched(s3):
    owner, alex, _, run, row = await _setup()
    tailored = await ResumeApplication(
        userId=owner.id, accountId=alex.id, companyName="Acme", companyNameLower="acme", jobDescription=JD,
        status="completed", source="job_applies",
    ).insert()
    await JobApplyRow.get_motor_collection().update_one({"_id": row.id}, {"$set": {f"tailored.{alex.id}": tailored.id}})
    row = await JobApplyRow.get(row.id)

    assert await fixed_forks.sync_row(row, run.threshold) == 0
    assert (await _slot(row, alex))[0] == tailored.id and s3["copied"] == []


async def test_below_the_minimum_closed_profile_or_non_pdf_gets_no_fork(s3):
    owner, alex, (_, _, notes), run, row = await _setup(total=60)
    assert await fixed_forks.sync_row(row, run.threshold) == 0  # below the minimum

    row.bestByProfile = [_score(alex, notes, 90)]
    assert await fixed_forks.sync_row(row, run.threshold) == 0  # a Markdown resume: the shared file, as before

    row.bestByProfile = [ResumeScore(**{**_score(alex, notes, 90).model_dump(), "resumeId": str(alex.resumes[0].id)})]
    row.profileGates = [ProfileGates(accountId=str(alex.id), gates=[{"name": "location", "result": "fail"}])]
    assert await fixed_forks.sync_row(row, run.threshold) == 0  # the job isn't open to this profile
    assert s3["copied"] == []


async def test_backfill_dry_run_writes_nothing_then_apply_is_idempotent(s3):
    from scripts.backfill_fixed_forks import backfill

    owner, alex, _, run, row = await _setup()

    assert (await backfill())["forks"] == 1
    assert (await _slot(row, alex))[0] is None and s3["copied"] == []

    assert (await backfill(apply=True))["forks"] == 1
    assert (await _slot(row, alex))[0] is not None
    assert (await backfill(apply=True))["forks"] == 0
