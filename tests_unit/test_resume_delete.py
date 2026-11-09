"""Deleting a generated resume (in-memory MongoDB via mongomock-motor): one an interview links to needs `force`."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")
os.environ["RESUME_QUEUE_DISABLED"] = "1"

from datetime import datetime

import pytest
from beanie import init_beanie
from fastapi import HTTPException
from mongomock_motor import AsyncMongoMockClient

from app.models import Account, Interview, ResumeApplication, UserExtra
from app.models.interview import InterviewResumeLink
from app.routers.resume import delete_job

pytestmark = [  # pre-existing utcnow defaults (app/models/base.py) and Beanie internals
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]


@pytest.fixture
async def db():
    await init_beanie(
        database=AsyncMongoMockClient()["resume-delete-unit"],
        document_models=[UserExtra, Account, Interview, ResumeApplication],
    )


async def _job():
    owner = await UserExtra(name="Athen", email="athen@x.io").insert()
    acc = await Account(name="Ian Maley", createdBy=owner.id).insert()
    job = await ResumeApplication(
        userId=owner.id, accountId=acc.id, profileName=acc.name, companyName="Limble", companyNameLower="limble",
        jobDescription="jd",
        status="completed",
    ).insert()
    return owner, acc, job


async def _interview(owner, acc, job, company="Limble"):
    return await Interview(
        accountId=acc.id, createdBy=owner.id, companyName=company, scheduledAt=datetime(2026, 11, 10, 15),
        resumeLink=InterviewResumeLink(kind="tailored", resumeJobId=job.id),
    ).insert()


async def test_unlinked_resume_is_deleted(db):
    owner, _, job = await _job()

    assert await delete_job(str(job.id), owner) == {"ok": True}
    assert await ResumeApplication.get(job.id) is None


async def test_linked_resume_is_kept_and_the_interview_is_named(db):
    owner, acc, job = await _job()
    await _interview(owner, acc, job)

    with pytest.raises(HTTPException) as err:
        await delete_job(str(job.id), owner)

    assert err.value.status_code == 409
    assert "Limble" in err.value.detail
    assert await ResumeApplication.get(job.id) is not None


async def test_several_linked_interviews_are_all_named(db):
    owner, acc, job = await _job()
    await _interview(owner, acc, job, "Limble")
    await _interview(owner, acc, job, "Acme")

    with pytest.raises(HTTPException) as err:
        await delete_job(str(job.id), owner)

    assert "Limble" in err.value.detail and "Acme" in err.value.detail


async def test_force_deletes_a_linked_resume_and_keeps_the_link(db):
    owner, acc, job = await _job()
    iv = await _interview(owner, acc, job)

    assert await delete_job(str(job.id), owner, force=True) == {"ok": True}

    assert await ResumeApplication.get(job.id) is None
    assert (await Interview.get(iv.id)).resumeLink.resumeJobId == job.id  # its frozen job description is still used


async def test_another_user_cannot_delete_or_learn_of_the_link(db):
    owner, acc, job = await _job()
    await _interview(owner, acc, job)
    other = await UserExtra(name="Other", email="other@x.io").insert()

    with pytest.raises(HTTPException) as err:
        await delete_job(str(job.id), other, force=True)

    assert err.value.status_code == 404
    assert await ResumeApplication.get(job.id) is not None
