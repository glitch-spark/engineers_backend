"""Correcting a Worth applying job's info (PUT /job-applies/rows/{id}/info) keeps it Worth applying: the posted date
the job site gave is kept when the person doesn't change it (in-memory MongoDB via mongomock-motor)."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")
os.environ["JOB_APPLY_WORKER_DISABLED"] = "1"

from datetime import date, datetime, timedelta

import pytest
from beanie import init_beanie
from mongomock_motor import AsyncMongoMockClient

from app.job_apply import extract as extract_mod
from app.job_apply import worker
from app.job_apply.schema import JobExtraction, Location
from app.models import ResumeApplication, UserExtra
from app.models.job_apply import (
    JobApplicationLog,
    JobApproval,
    JobApplyRow,
    JobApplyRun,
    JobExtractionCache,
    JobInfo,
    JobInvalidRole,
)
from app.routers.job_applies import InfoBody, update_row_info

pytestmark = [
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]

URL = "https://jobs.example.com/42"
JD = "Senior full stack engineer, React and Node, fully remote in the US. " * 6
# The AI reading finds no posted date: the job site's data gave it (fetchedPostedDate).
REMOTE_US = JobExtraction(
    title="Full Stack Engineer", company="Acme", workMode="remote",
    allowedLocations=[Location(kind="country", value="US")], postedDate=None,
)


@pytest.fixture
async def db():
    database = AsyncMongoMockClient()["edit-keeps-bucket"]
    await init_beanie(
        database=database,
        document_models=[
            UserExtra, JobApplyRun, JobApplyRow, JobInfo, JobApproval, JobInvalidRole, JobExtractionCache,
            JobApplicationLog, ResumeApplication,
        ],
    )
    key = f"{extract_mod.jd_hash(JD)}:{extract_mod.PROMPT_VERSION}"  # cached reading: no LLM call
    await JobExtractionCache(key=key, extraction=REMOTE_US).insert()
    return database


async def worth_applying_row():
    user = await UserExtra(name="Pedro", email="pedro@x.io", role="staff").insert()
    run = await JobApplyRun(userId=user.id, fileName="sheet", status="screened", screenedAt=datetime.utcnow()).insert()
    row = await JobApplyRow(
        runId=run.id, userId=user.id, rowIndex=2, url=URL, jdText=JD, jdSource="ats_api", jdStructured=True,
        status="fetched", fetchedPostedDate=date.today() - timedelta(days=2),
    ).insert()
    await worker._extract_one(row, run, None)
    await row.save()
    return user, row


async def test_editing_the_title_keeps_a_worth_applying_job_there(db):
    user, row = await worth_applying_row()
    assert row.screen == "valid" and row.postedDate == date.today() - timedelta(days=2)

    await update_row_info(str(row.id), InfoBody(title="Senior Full Stack Engineer"), user)

    fresh = await JobApplyRow.get(row.id)
    assert fresh.title == "Senior Full Stack Engineer"
    assert fresh.postedDate == date.today() - timedelta(days=2)  # the job site's date is kept
    assert fresh.screen == "valid"


async def test_a_pasted_description_is_read_even_with_other_corrections(db, monkeypatch):
    """A "Not a job page" reading (an error page: no terms) plus a pasted description AND a title edit: the new
    description is read, so its terms replace the empty ones, and the typed title still wins."""
    from app.job_apply.schema import Term

    user = await UserExtra(name="Pedro", email="pedro@x.io", role="staff").insert()
    run = await JobApplyRun(userId=user.id, fileName="sheet", status="screened", screenedAt=datetime.utcnow()).insert()
    error_page = JobExtraction(isJobPosting=False)
    row = await JobApplyRow(
        runId=run.id, userId=user.id, rowIndex=2, url=URL, jdText="Page not found", jdSource="html",
        status="extracted", extraction=error_page, extractionSource="llm",
    ).insert()
    pasted = "Backend engineer. Requirements: Python, PostgreSQL. Fully remote in the US. " * 4
    reads = []

    async def fake_extract(jd, *, user, run_date):
        reads.append(jd)
        return REMOTE_US.model_copy(update={"terms": [Term(term="Python", tier="required")]}), "llm"

    monkeypatch.setattr(worker, "EXTRACT", fake_extract)

    await update_row_info(str(row.id), InfoBody(jdText=pasted, title="Backend Engineer"), user)

    assert reads == [pasted.strip()]
    fresh = await JobApplyRow.get(row.id)
    assert [t.term for t in fresh.extraction.terms] == ["Python"]
    assert fresh.extraction.isJobPosting and fresh.title == "Backend Engineer"


async def test_a_changed_posted_date_is_still_taken(db):
    user, row = await worth_applying_row()
    old = date.today() - timedelta(days=400)

    await update_row_info(str(row.id), InfoBody(postedDate=old), user)

    fresh = await JobApplyRow.get(row.id)
    assert fresh.postedDate == old and fresh.screen == "too_old"
