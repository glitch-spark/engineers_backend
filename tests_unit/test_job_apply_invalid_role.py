"""Moving a Worth applying job to Invalid role (in-memory MongoDB via mongomock-motor): only from Worth applying,
before or after scoring (it then leaves the Suggested list), undone back to where it was, and shared per job link like
an approval."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")
os.environ["JOB_APPLY_WORKER_DISABLED"] = "1"

from datetime import datetime

import pytest
from beanie import init_beanie
from fastapi import HTTPException
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
from app.routers.job_applies import (
    IncludeBody, InvalidBody, _rows_query, _suggested_count, include_row, mark_invalid_row,
)

pytestmark = [  # pre-existing utcnow defaults (app/models/base.py) and Beanie internals
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]

URL = "https://jobs.example.com/1"
REMOTE_JD = "Senior backend engineer, Python and AWS, fully remote in the US. " * 6
ONSITE_JD = "Senior backend engineer, Python and AWS, on site in Austin. " * 6


def _reading(work_mode: str) -> JobExtraction:
    return JobExtraction(
        title="Backend Engineer", company="Example Inc", workMode=work_mode,
        allowedLocations=[Location(kind="country", value="US")], postedDate=datetime.utcnow().date(),
    )


@pytest.fixture
async def db():
    database = AsyncMongoMockClient()["invalid-role-unit"]  # fresh in-memory database per test
    await init_beanie(
        database=database,
        document_models=[
            UserExtra, JobApplyRun, JobApplyRow, JobInfo, JobApproval, JobInvalidRole, JobExtractionCache,
            JobApplicationLog, ResumeApplication,
        ],
    )
    for jd, mode in ((REMOTE_JD, "remote"), (ONSITE_JD, "onsite")):  # cached readings: no LLM call
        await JobExtractionCache(key=f"{extract_mod.jd_hash(jd)}:{extract_mod.PROMPT_VERSION}", extraction=_reading(mode)).insert()
    return database


async def _read_run(name: str, *, jd: str = REMOTE_JD, status: str = "screened"):
    """A user's run of the sheet with the one job read (remote: Worth applying; on-site: not)."""
    user = await UserExtra(name=name, email=f"{name.lower()}@x.io", role="staff").insert()
    run = await JobApplyRun(userId=user.id, fileName="sheet", status=status, screenedAt=datetime.utcnow()).insert()
    row = await JobApplyRow(
        runId=run.id, userId=user.id, rowIndex=2, url=URL, jdText=jd, jdSource="sheet", jdStructured=True,
        status="fetched",
    ).insert()
    await worker._extract_one(row, run, None)
    await row.save()
    return user, run, row


async def test_a_worth_applying_job_moves_to_invalid_role(db):
    user, _, row = await _read_run("Puma")
    assert row.screen == "valid"

    out = await mark_invalid_row(str(row.id), InvalidBody(invalid=True), user)

    fresh = await JobApplyRow.get(row.id)
    assert (fresh.markedInvalid, fresh.screen) == (True, "invalid_role")
    assert (out["markedInvalid"], out["screen"]) == (True, "invalid_role")
    mark = await JobInvalidRole.find_one(JobInvalidRole.url == URL)
    assert (mark.markedBy, mark.markedByName) == (user.id, "Puma")


async def test_undo_moves_it_back_to_worth_applying(db):
    user, _, row = await _read_run("Puma")
    await mark_invalid_row(str(row.id), InvalidBody(invalid=True), user)

    out = await mark_invalid_row(str(row.id), InvalidBody(invalid=False), user)

    assert (out["markedInvalid"], out["screen"]) == (False, "valid")
    assert await JobInvalidRole.find_all().count() == 0


async def test_an_approved_job_comes_back_approved(db):
    user, _, row = await _read_run("Puma", jd=ONSITE_JD)
    await include_row(str(row.id), IncludeBody(include=True), user)

    out = await mark_invalid_row(str(row.id), InvalidBody(invalid=True), user)
    assert out["screen"] == "invalid_role"

    out = await mark_invalid_row(str(row.id), InvalidBody(invalid=False), user)
    assert (out["forceInclude"], out["screen"]) == (True, "valid")


async def test_only_worth_applying_jobs_can_be_moved(db):
    user, _, row = await _read_run("Puma", jd=ONSITE_JD)
    assert row.screen == "onsite"

    with pytest.raises(HTTPException) as err:
        await mark_invalid_row(str(row.id), InvalidBody(invalid=True), user)

    assert err.value.status_code == 400
    assert (await JobApplyRow.get(row.id)).screen == "onsite"


async def _scored(row: JobApplyRow, score: int = 90) -> None:
    row.status, row.topScore, row.scoredAt = "scored", score, datetime.utcnow()
    await row.save()


async def _suggested_ids(run: JobApplyRun) -> list:
    return [r.id for r in await JobApplyRow.find(await _rows_query(run, view="suggested")).to_list()]


async def test_a_scored_job_can_be_moved_and_leaves_the_suggested_list(db):
    user, run, row = await _read_run("Puma", status="done")
    await _scored(row)
    assert await _suggested_ids(run) == [row.id] and await _suggested_count(run) == 1

    out = await mark_invalid_row(str(row.id), InvalidBody(invalid=True), user)

    assert out["screen"] == "invalid_role"
    assert await _suggested_ids(run) == [] and await _suggested_count(run) == 0


async def test_undo_after_scoring_brings_it_back_with_its_score(db):
    user, run, row = await _read_run("Puma", status="done")
    await _scored(row, 88)
    await mark_invalid_row(str(row.id), InvalidBody(invalid=True), user)

    out = await mark_invalid_row(str(row.id), InvalidBody(invalid=False), user)

    assert (out["screen"], (await JobApplyRow.get(row.id)).topScore) == ("valid", 88)
    assert await _suggested_ids(run) == [row.id] and await _suggested_count(run) == 1


async def test_an_invalid_role_reaches_other_runs_of_the_same_link(db):
    puma, _, puma_row = await _read_run("Puma")
    _, nick_run, nick_row = await _read_run("Nicholas")

    await mark_invalid_row(str(puma_row.id), InvalidBody(invalid=True), puma)
    assert await worker.sync_corrections(await JobApplyRun.get(nick_run.id)) == 1

    fresh = await JobApplyRow.get(nick_row.id)
    assert (fresh.markedInvalid, fresh.screen) == (True, "invalid_role")


async def test_a_run_uploaded_after_it_reads_the_job_as_invalid(db):
    puma, _, puma_row = await _read_run("Puma")
    await mark_invalid_row(str(puma_row.id), InvalidBody(invalid=True), puma)

    _, _, later = await _read_run("Athen")

    assert (later.markedInvalid, later.screen) == (True, "invalid_role")


async def test_undoing_it_withdraws_it_for_new_runs(db):
    puma, _, puma_row = await _read_run("Puma")
    await mark_invalid_row(str(puma_row.id), InvalidBody(invalid=True), puma)
    await mark_invalid_row(str(puma_row.id), InvalidBody(invalid=False), puma)

    _, _, later = await _read_run("Athen")

    assert (later.markedInvalid, later.screen) == (False, "valid")
