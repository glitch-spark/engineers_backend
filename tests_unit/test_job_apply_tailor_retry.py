"""Job Applies tailoring against an in-memory MongoDB (mongomock-motor): retrying a failed tailored resume re-runs
the same job instead of adding another one to Generated Resumes."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")
os.environ["RESUME_QUEUE_DISABLED"] = "1"

from datetime import datetime, timezone

import pytest
from beanie import init_beanie
from mongomock_motor import AsyncMongoMockClient

from app import resume_queue
from app.models import Account, ResumeApplication, UserExtra
from app.models.job_apply import JobApplicationLog, JobApplyRow, JobApplyRun, ProfileGates, RunSelection
from app.routers.job_applies import TailorAllBody, _tailor_row, tailor_all

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
