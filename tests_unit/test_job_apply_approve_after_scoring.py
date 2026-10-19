"""Approving a Needs a check job after the run was scored (PUT /job-applies/rows/{id}/include): allowed, and the job is
scored on the spot; refused only while a step is running (in-memory MongoDB via mongomock-motor)."""

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
from app.routers.job_applies import IncludeBody, _rows_query, include_row

pytestmark = [
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]

URL = "https://jobs.example.com/7"
JD = "Backend engineer, Python, on site in Austin three days a week. " * 6
ONSITE = JobExtraction(title="Backend Engineer", company="Acme", workMode="onsite",
                       allowedLocations=[Location(kind="country", value="US")])


@pytest.fixture
async def db(monkeypatch):
    database = AsyncMongoMockClient()["approve-after-scoring"]
    await init_beanie(
        database=database,
        document_models=[
            UserExtra, JobApplyRun, JobApplyRow, JobInfo, JobApproval, JobInvalidRole, JobExtractionCache,
            JobApplicationLog, ResumeApplication,
        ],
    )
    key = f"{extract_mod.jd_hash(JD)}:{extract_mod.PROMPT_VERSION}"
    await JobExtractionCache(key=key, extraction=ONSITE).insert()
    return database


@pytest.fixture
def scored(monkeypatch):
    """Records which rows the score stage was asked to score (no resumes or profiles needed)."""
    calls = []

    async def fake_score_stage(run, rows, check, only=None):
        calls.append(only)
        for r in rows:
            if only is None or r.id in only:
                await JobApplyRow.get_motor_collection().update_one(
                    {"_id": r.id}, {"$set": {"status": "scored", "scoredAt": datetime.utcnow(), "topScore": 81}})

    monkeypatch.setattr(worker, "_score_stage", fake_score_stage)
    return calls


async def job_in_check(run_status: str, phase: str):
    user = await UserExtra(name="Pedro", email="pedro@x.io", role="staff").insert()
    run = await JobApplyRun(userId=user.id, fileName="sheet", status=run_status, phase=phase,
                            screenedAt=datetime.utcnow()).insert()
    row = await JobApplyRow(runId=run.id, userId=user.id, rowIndex=2, url=URL, jdText=JD, jdSource="sheet",
                            jdStructured=True, status="fetched").insert()
    await worker._extract_one(row, run, None)
    await row.save()
    assert row.screen == "onsite"  # an approvable bucket, like Needs a check
    return user, run, row


async def test_approving_after_scoring_moves_it_and_scores_it(db, scored):
    user, run, row = await job_in_check("done", "score")

    out = await include_row(str(row.id), IncludeBody(include=True), user)

    fresh = await JobApplyRow.get(row.id)
    assert (fresh.forceInclude, fresh.screen) == (True, "valid")
    assert scored == [{row.id}]  # only this job is scored
    assert fresh.topScore == 81 and out["topScore"] == 81


async def test_approving_before_scoring_does_not_score(db, scored):
    user, run, row = await job_in_check("screened", "screen")
    await include_row(str(row.id), IncludeBody(include=True), user)
    assert (await JobApplyRow.get(row.id)).screen == "valid" and scored == []


@pytest.mark.parametrize("status", ["queued", "running"])
async def test_approving_waits_while_a_step_runs(db, scored, status):
    user, run, row = await job_in_check(status, "score")
    with pytest.raises(HTTPException) as err:
        await include_row(str(row.id), IncludeBody(include=True), user)
    assert err.value.status_code == 409 and err.value.detail == "Wait for the run to finish its current step."


async def test_an_approval_undone_after_scoring_leaves_suggested(db, scored):
    user, run, row = await job_in_check("done", "score")
    await include_row(str(row.id), IncludeBody(include=True), user)
    await include_row(str(row.id), IncludeBody(include=False), user)

    fresh = await JobApplyRow.get(row.id)
    assert fresh.screen == "onsite" and fresh.topScore == 81  # its score stays for a later approval
    suggested = await JobApplyRow.find(await _rows_query(run, view="suggested")).to_list()
    assert suggested == []
