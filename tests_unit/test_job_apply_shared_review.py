"""What one person settles about a job link is shared with everyone who uses that link (in-memory MongoDB via
mongomock-motor): approvals into Worth applying, and the sheet's own company name over machine readings."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")
os.environ["JOB_APPLY_WORKER_DISABLED"] = "1"

from datetime import datetime

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
)
from app.routers.job_applies import IncludeBody, include_row

pytestmark = [  # pre-existing utcnow defaults (app/models/base.py) and Beanie internals
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]

URL = "https://jobs.example.com/1"
JD = "Senior backend engineer, Python and AWS, on site in Austin. " * 6
ONSITE = JobExtraction(
    title="Backend Engineer", company="Example Inc (AI)", workMode="onsite",
    allowedLocations=[Location(kind="country", value="US")],
)


@pytest.fixture
async def db():
    database = AsyncMongoMockClient()["shared-review-unit"]  # fresh in-memory database per test
    await init_beanie(
        database=database,
        document_models=[
            UserExtra, JobApplyRun, JobApplyRow, JobInfo, JobApproval, JobExtractionCache, JobApplicationLog,
            ResumeApplication,
        ],
    )
    key = f"{extract_mod.jd_hash(JD)}:{extract_mod.PROMPT_VERSION}"  # the reading is cached: no LLM call
    await JobExtractionCache(key=key, extraction=ONSITE).insert()
    return database


async def _read_run(name: str, *, company: str = "") -> tuple[UserExtra, JobApplyRun, JobApplyRow]:
    """A user's run of the sheet, screened, with the job read as on-site (not worth applying)."""
    user = await UserExtra(name=name, email=f"{name.lower()}@x.io", role="staff").insert()
    run = await JobApplyRun(userId=user.id, fileName="sheet", status="screened", screenedAt=datetime.utcnow()).insert()
    row = await JobApplyRow(
        runId=run.id, userId=user.id, rowIndex=2, url=URL, jdText=JD, jdSource="sheet", jdStructured=True,
        status="fetched", company=company, sheetCompany=company,
    ).insert()
    await worker._extract_one(row, run, None)
    await row.save()
    return user, run, row


async def test_an_approval_reaches_other_runs_of_the_same_link(db):
    nick, nick_run, nick_row = await _read_run("Nicholas")
    yuari, yuari_run, yuari_row = await _read_run("Yuari")
    assert (nick_row.screen, yuari_row.screen) == ("onsite", "onsite")

    await include_row(str(nick_row.id), IncludeBody(include=True), nick)
    assert await worker.sync_corrections(await JobApplyRun.get(yuari_run.id)) == 1

    fresh = await JobApplyRow.get(yuari_row.id)
    assert (fresh.forceInclude, fresh.screen) == (True, "valid")


async def test_a_run_uploaded_after_an_approval_reads_the_job_as_worth_applying(db):
    nick, _, nick_row = await _read_run("Nicholas")
    await include_row(str(nick_row.id), IncludeBody(include=True), nick)

    _, _, later = await _read_run("Puma")

    assert (later.forceInclude, later.screen) == (True, "valid")


async def test_undoing_an_approval_withdraws_it_for_new_runs(db):
    nick, _, nick_row = await _read_run("Nicholas")
    await include_row(str(nick_row.id), IncludeBody(include=True), nick)
    await include_row(str(nick_row.id), IncludeBody(include=False), nick)

    assert await JobApproval.find_all().count() == 0
    _, _, later = await _read_run("Puma")
    assert (later.forceInclude, later.screen) == (False, "onsite")


async def test_the_sheets_company_beats_the_ai_reading_and_corrections(db):
    _, run, row = await _read_run("Athen", company="Acme Corp")
    assert row.company == "Acme Corp"  # not the reading's "Example Inc (AI)"

    await JobInfo(url=URL, jdText=JD, extraction=ONSITE, title="Backend Engineer", company="Example Inc (AI)").insert()
    await worker.reread_with_info(run, [row])

    assert (await JobApplyRow.get(row.id)).company == "Acme Corp"


async def test_without_a_sheet_company_the_reading_fills_it(db):
    _, _, row = await _read_run("Athen")
    assert row.company == "Example Inc (AI)"
