"""Marking a job applied with an uploaded resume keeps the JD and that resume's score on the permanent applied log,
so an interview can be linked to it after the run expires (in-memory MongoDB via mongomock-motor)."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")
os.environ["RESUME_QUEUE_DISABLED"] = "1"

import pytest
from beanie import init_beanie
from mongomock_motor import AsyncMongoMockClient

from app.job_apply.schema import ResumeScore, TermHit
from app.models import Account, ResumeApplication, UserExtra
from app.models.account import Resume
from app.models.job_apply import JobApplicationLog, JobApplyRow, JobApplyRun, JobPostingCache
from app.routers.job_applies import AppliedBody, set_row_applied

pytestmark = [  # pre-existing utcnow defaults (app/models/base.py) and Beanie internals
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]


def _hit(term, weight, credit):
    return TermHit(term=term, tier="required", weight=weight, credit=credit, match="missing" if credit == 0 else "exact",
                   where="none" if credit == 0 else "recent")


@pytest.fixture
async def setup():
    database = AsyncMongoMockClient()["applied-snapshot-unit"]
    await init_beanie(database=database, document_models=[
        UserExtra, Account, JobApplyRun, JobApplyRow, JobApplicationLog, JobPostingCache, ResumeApplication,
    ])
    owner = await UserExtra(name="Athen", email="athen@x.io").insert()
    cv = Resume(filename="alex_backend.pdf", markdown="x", s3Key="k/a1.pdf")
    alex = await Account(name="Alex", createdBy=owner.id, resumes=[cv]).insert()
    run = await JobApplyRun(userId=owner.id, fileName="jobs.csv", status="done", threshold=75).insert()
    score = ResumeScore(
        accountId=str(alex.id), resumeId=str(cv.id), filename=cv.filename, total=82, band="good",
        knockouts=["Missing required: Kafka"],
        terms=[_hit("Python", 3.0, 1.0), _hit("Kafka", 2.0, 0.0), _hit("Terraform", 1.0, 0.0)],
    )
    url = "https://jobs.example.com/a"
    # The JD lives in the posting cache, as most fetched rows' do.
    await JobPostingCache(url=url, text="Senior Python role with Kafka", source="fetch").insert()
    row = await JobApplyRow(runId=run.id, userId=owner.id, rowIndex=0, url=url, company="Acme", status="scored",
                            jdSource="html", scores=[score]).insert()
    return owner, alex, cv, row


async def test_uploaded_resume_application_keeps_jd_and_score(setup):
    owner, alex, cv, row = setup

    await set_row_applied(str(row.id), AppliedBody(accountId=str(alex.id), resumeId=str(cv.id), applied=True), owner)

    entry = await JobApplicationLog.find_one(JobApplicationLog.rowId == row.id)
    assert entry.jdText == "Senior Python role with Kafka"
    assert entry.score.total == 82 and entry.score.band == "good"
    assert entry.score.missing == ["Kafka", "Terraform"]  # heaviest first
    assert entry.score.knockouts == ["Missing required: Kafka"]
