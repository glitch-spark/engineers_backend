"""The Resume page's list shows a Job Applies resume's match score, the number beside it on Job Applies (in-memory
MongoDB via mongomock-motor); ones from before scoring are scored when listed."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")
os.environ["RESUME_QUEUE_DISABLED"] = "1"

import pytest
from beanie import init_beanie
from mongomock_motor import AsyncMongoMockClient

from app.job_apply import tailored_score
from app.job_apply.schema import TailoredMatch
from app.models import Account, ResumeApplication, UserExtra
from app.models.job_apply import JobApplyRow, JobApplyRun
from app.routers.resume import list_jobs

pytestmark = [  # pre-existing utcnow defaults (app/models/base.py) and Beanie internals
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]

MATCH = TailoredMatch(total=88, band="strong", missing=["Kafka"])


@pytest.fixture
async def db(monkeypatch):
    database = AsyncMongoMockClient()["resume-list-unit"]
    await init_beanie(database=database, document_models=[UserExtra, Account, JobApplyRun, JobApplyRow, ResumeApplication])
    scored: list = []

    async def fake_score(row_id, text):
        scored.append(text)
        return MATCH if text == "scorable" else None

    monkeypatch.setattr(tailored_score, "score_text", fake_score)
    return scored


async def _resume(owner, company, **kw) -> ResumeApplication:
    return await ResumeApplication(
        userId=owner.id, profileName="Jordan", companyName=company, companyNameLower=company.lower(),
        jobDescription="jd", **kw,
    ).insert()


def _by_company(out) -> dict:
    return {j["companyName"]: j["matchScore"] for j in out["jobs"]}


async def test_lists_the_stored_score_and_none_for_generator_resumes(db):
    owner = await UserExtra(name="Athen", email="athen@x.io").insert()
    await _resume(owner, "Acme", status="completed", source="job_applies", matchScore=MATCH)
    await _resume(owner, "Globex", status="completed", source="generator", plainText="scorable")

    out = await list_jobs(owner)

    assert _by_company(out) == {"Acme": MATCH.model_dump(), "Globex": None}
    assert db == []  # nothing needed scoring


async def test_scores_older_job_applies_resumes_once(db):
    owner = await UserExtra(name="Athen", email="athen@x.io").insert()
    old = await _resume(owner, "Acme", status="completed", source="job_applies", plainText="scorable")
    await _resume(owner, "Initech", status="completed", source="job_applies", plainText="no job")
    await _resume(owner, "Hooli", status="in_progress", source="job_applies", plainText="scorable")
    # Unset, not null: as stored before scoring existed.
    await ResumeApplication.get_motor_collection().update_many({}, {"$unset": {"matchScore": ""}})

    out = await list_jobs(owner)

    assert _by_company(out) == {"Acme": MATCH.model_dump(), "Initech": None, "Hooli": None}
    assert sorted(db) == ["no job", "scorable"]  # the unfinished one isn't scored
    assert (await ResumeApplication.get(old.id)).matchScore == MATCH

    db.clear()
    await list_jobs(owner)
    assert db == []  # stored, including the null for the one that couldn't be scored
