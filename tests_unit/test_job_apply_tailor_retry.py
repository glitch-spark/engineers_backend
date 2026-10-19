"""Job Applies tailoring against an in-memory MongoDB (mongomock-motor): retrying a failed tailored resume re-runs
the same job instead of adding another one to Generated Resumes."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")
os.environ["RESUME_QUEUE_DISABLED"] = "1"

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from beanie import init_beanie
from mongomock_motor import AsyncMongoMockClient

from app import resume_queue
from app.job_apply.schema import JobExtraction
from app.models import Account, ResumeApplication, UserExtra
from app.models.job_apply import JobApplicationLog, JobApplyRow, JobApplyRun, ProfileGates, RunSelection
from app.routers import resume as resume_router
from app.routers.job_applies import TailorAllBody, TailorBody, _tailor_row, tailor_all, tailor_row

pytestmark = [  # pre-existing utcnow defaults (app/models/base.py) and Beanie internals
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]


@pytest.fixture
async def db():
    database = AsyncMongoMockClient()["tailor-unit"]  # fresh in-memory database per test
    await init_beanie(
        database=database,
        document_models=[UserExtra, Account, JobApplyRun, JobApplyRow, ResumeApplication, JobApplicationLog],
    )
    return database


async def _setup(previous_status: str):
    owner = await UserExtra(name="Athen", email="athen@x.io").insert()
    account = await Account(name="Ian Meron", createdBy=owner.id, styleTemplate="<html>{{resume}}</html>").insert()
    run = await JobApplyRun(userId=owner.id, fileName="jobs.csv", selection=[RunSelection(accountId=account.id)]).insert()
    previous = await ResumeApplication(
        userId=owner.id, accountId=account.id, profileName="Ian Meron", companyName="Acme", companyNameLower="acme",
        jobDescription="old description", status=previous_status, step="generating_resume",
        errorMessage="Model 'anthropic:claude-opus-5-5' is not configured on this server." if previous_status == "failed" else None,
        source="job_applies", countsAsBid=False, attempts=1, resumeModelId="anthropic:claude-opus-5-5",
        completedAt=datetime.now(timezone.utc),
    ).insert()
    row = await JobApplyRow(
        runId=run.id, userId=owner.id, rowIndex=0, url="https://jobs.example.com/1", company="Acme",
        jdText="We need a backend engineer.", tailored={str(account.id): previous.id},
        profileGates=[ProfileGates(accountId=str(account.id))],
    ).insert()
    previous_row = await JobApplyRow.get(row.id)  # as the endpoint loads it
    return owner, account, run, previous_row, previous


async def test_retry_failed_tailor_requeues_the_same_job(db):
    owner, account, run, row, failed = await _setup("failed")

    job = await _tailor_row(row, run, owner, str(account.id), False, resume_model="openai:gpt-4o-mini")

    assert job.id == failed.id
    assert await ResumeApplication.find_all().count() == 1  # no second entry in Generated Resumes
    again = await ResumeApplication.get(failed.id)
    assert (again.status, again.step, again.errorMessage, again.attempts) == ("queued", "queued", None, 0)
    assert again.resumeModelId == "openai:gpt-4o-mini"
    assert again.jobDescription == "We need a backend engineer."  # the job's current description
    assert (await JobApplyRow.get(row.id)).tailored == {str(account.id): failed.id}


async def test_retailor_completed_keeps_it_and_adds_a_new_job(db):
    owner, account, run, row, done = await _setup("completed")

    job = await _tailor_row(row, run, owner, str(account.id), False)

    assert job.id != done.id
    assert (await ResumeApplication.get(done.id)).status == "completed"  # earlier version stays in Generated Resumes
    assert await ResumeApplication.find_all().count() == 2
    assert (await JobApplyRow.get(row.id)).tailored == {str(account.id): job.id}


@pytest.mark.parametrize("dry_run", [True, False])
async def test_tailor_all_at_the_cap_still_retries_failed_ones(db, monkeypatch, dry_run):
    owner, account, run, failed_row, failed = await _setup("failed")
    fresh_row = await JobApplyRow(
        runId=run.id, userId=owner.id, rowIndex=1, url="https://jobs.example.com/2", company="Globex",
        jdText="We need a frontend engineer.", profileGates=[ProfileGates(accountId=str(account.id))],
    ).insert()
    monkeypatch.setattr(resume_queue, "_daily_cap", lambda: 1)  # the failed job already used today's only slot

    body = TailorAllBody(rowIds=[str(failed_row.id), str(fresh_row.id)], dryRun=dry_run)
    result = await tailor_all(str(run.id), body, owner)

    assert result == {"queued": 1, "skippedCap": 1, "skipped": 0}  # the retry adds no job, so the cap doesn't stop it
    if not dry_run:
        assert (await ResumeApplication.get(failed.id)).status == "queued"
        assert await ResumeApplication.find_all().count() == 1


async def test_job_applies_always_tailors_with_gpt_4o_mini(db):
    owner, account, run, _row, _done = await _setup("completed")
    rows = [
        await JobApplyRow(
            runId=run.id, userId=owner.id, rowIndex=i, url=f"https://jobs.example.com/{i}", company="Globex",
            jdText="We need a frontend engineer.", profileGates=[ProfileGates(accountId=str(account.id))],
        ).insert()
        for i in (1, 2)
    ]
    other = "anthropic:claude-opus-5-5"  # whatever was picked elsewhere is ignored

    await tailor_row(str(rows[0].id), TailorBody.model_validate({"accountId": str(account.id), "resumeModel": other}), owner)
    await tailor_all(str(run.id), TailorAllBody.model_validate({"rowIds": [str(rows[1].id)], "resumeModel": other}), owner)

    for row in rows:
        job = await ResumeApplication.get((await JobApplyRow.get(row.id)).tailored[str(account.id)])
        assert job.resumeModelId == "openai:gpt-4o-mini"


_FULL_JD = (
    "Senior Backend Engineer\nRequirements:\n- 5+ years of experience with Python and PostgreSQL\n"
    "- Bachelor's degree or equivalent experience\n- Active Secret clearance required\n"
    "Nice to have:\n- Kubernetes\nThis is a hybrid role, 3 days in office.\nPosted 10 days ago"
)
_SHORT_JD = "Senior Backend Engineer\n- 5+ years of experience with Python and PostgreSQL\n- Kubernetes"


async def test_tailor_stores_short_and_full_description(db):
    owner, account, run, _row, _done = await _setup("completed")
    row = await JobApplyRow(
        runId=run.id, userId=owner.id, rowIndex=3, url="https://jobs.example.com/9", company="Acme",
        jdText=_FULL_JD, extraction=JobExtraction(title="Senior Backend Engineer", tailorJd=_SHORT_JD),
        profileGates=[ProfileGates(accountId=str(account.id))],
    ).insert()

    job = await _tailor_row(row, run, owner, str(account.id), True)

    assert job.jobDescription == _FULL_JD
    assert job.tailorJobDescription == _SHORT_JD


async def test_resume_prompt_gets_short_text_and_cover_letter_gets_full(db, monkeypatch):
    owner = await UserExtra(name="Athen", email="athen@x.io").insert()
    account = await Account(name="Ian Meron", createdBy=owner.id, styleTemplate="<html>{{resume}}</html>").insert()
    job = await ResumeApplication(
        userId=owner.id, accountId=account.id, profileName="Ian Meron", companyName="Acme", companyNameLower="acme",
        jobDescription=_FULL_JD, tailorJobDescription=_SHORT_JD, generateCoverLetter=True,
        screeningQuestions=["Why you?"], status="in_progress", step="generating_resume", source="job_applies",
    ).insert()
    seen = {}

    async def fake_generate(account, jd, company, **kwargs):
        seen["resume"] = jd
        return SimpleNamespace(
            cost_usd=None, input_tokens=1, output_tokens=1, reasoning_tokens=0, plain_text="resume text",
            llm_provider="openai", llm_model="x", llm_fallback_used=False, llm_fallback_reason=None,
            s3_key=None, pdf_bytes=b"%PDF",
        )

    async def fake_letter(**kwargs):
        seen["letter"] = kwargs["job_description"]
        return SimpleNamespace(
            text="Dear Acme", provider="openai", model="x", fallback_used=False, fallback_reason=None, cost_usd=None,
        )

    async def fake_screen(**kwargs):
        seen["screen"] = kwargs["jd"]
        return SimpleNamespace(
            pairs=[], cost_usd=None, llm_provider="openai", llm_model="x",
            llm_fallback_used=False, llm_fallback_reason=None,
        )

    async def noop(*_args, **_kwargs):
        return None

    monkeypatch.setattr(resume_router, "generate_resume", fake_generate)
    monkeypatch.setattr(resume_router, "write_cover_letter", fake_letter)
    monkeypatch.setattr(resume_router, "_run_screening", fake_screen)
    monkeypatch.setattr(resume_queue, "persist_job", noop)
    monkeypatch.setattr("app.job_apply.tailored_score.score_job", noop)
    monkeypatch.setattr("app.job_apply.sheet_links.fill_link", noop)

    await resume_router.run_job_doc(job)

    assert seen["resume"] == _SHORT_JD
    assert seen["letter"] == _FULL_JD
    assert seen["screen"] == _FULL_JD
