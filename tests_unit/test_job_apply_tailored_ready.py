"""GET /job-applies/runs/{id}/tailored?accountId=: a profile's completed tailored resumes in a run (mongomock)."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")
os.environ["JOB_APPLY_WORKER_DISABLED"] = "1"

from datetime import datetime

import pytest
from beanie import PydanticObjectId, init_beanie
from mongomock_motor import AsyncMongoMockClient

from app.models import ResumeApplication, UserExtra
from app.models.job_apply import JobApplyRow, JobApplyRun

pytestmark = [
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]

IAN, MARIA = PydanticObjectId(), PydanticObjectId()


@pytest.fixture
async def db():
    database = AsyncMongoMockClient()["tailored-ready-unit"]
    await init_beanie(database=database, document_models=[UserExtra, JobApplyRun, JobApplyRow, ResumeApplication])
    return database


async def resume(owner, account, status):
    return await ResumeApplication(
        userId=owner.id, accountId=account, profileName="P", companyName="Acme", companyNameLower="acme",
        jobDescription="jd", status=status, source="job_applies", countsAsBid=False,
    ).insert()


async def setup():
    owner = await UserExtra(name="Pedro", email="p@x.io", role="staff").insert()
    run = await JobApplyRun(userId=owner.id, fileName="sheet", status="screened", screenedAt=datetime(2026, 10, 8)).insert()
    done1, done2 = await resume(owner, IAN, "completed"), await resume(owner, IAN, "completed")
    queued, maria = await resume(owner, IAN, "queued"), await resume(owner, MARIA, "completed")
    for i, tailored in enumerate([{str(IAN): done1.id, str(MARIA): maria.id}, {str(IAN): done2.id}, {str(IAN): queued.id}]):
        await JobApplyRow(runId=run.id, userId=owner.id, rowIndex=i, url=f"https://jobs.example.com/{i}",
                          status="fetched", tailored=tailored).insert()
    other_run = await JobApplyRun(userId=owner.id, fileName="other", status="screened", screenedAt=datetime(2026, 10, 8)).insert()
    elsewhere = await resume(owner, IAN, "completed")
    await JobApplyRow(runId=other_run.id, userId=owner.id, rowIndex=0, url="https://jobs.example.com/x",
                      status="fetched", tailored={str(IAN): elsewhere.id}).insert()
    return owner, run, {"done": {str(done1.id), str(done2.id)}, "maria": str(maria.id)}


def client(user):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.deps import get_current_user
    from app.routers import job_applies

    app = FastAPI()
    app.include_router(job_applies.router)
    app.dependency_overrides[get_current_user] = lambda: user
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def get(user, run, account):
    async with client(user) as c:
        return await c.get(f"/job-applies/runs/{run.id}/tailored", params={"accountId": str(account)})


async def test_only_this_profiles_completed_resumes_in_this_run(db):
    owner, run, ids = await setup()
    r = await get(owner, run, IAN)
    assert r.status_code == 200, r.text
    assert set(r.json()["jobIds"]) == ids["done"]
    assert (await get(owner, run, MARIA)).json()["jobIds"] == [ids["maria"]]


async def test_admin_sees_it_and_others_get_404(db):
    owner, run, ids = await setup()
    admin = await UserExtra(name="Root", email="r@x.io", role="admin").insert()
    stranger = await UserExtra(name="Ana", email="a@x.io", role="staff").insert()
    assert set((await get(admin, run, IAN)).json()["jobIds"]) == ids["done"]
    assert (await get(stranger, run, IAN)).status_code == 404


async def test_unknown_profile_is_empty(db):
    owner, run, _ = await setup()
    assert (await get(owner, run, PydanticObjectId())).json()["jobIds"] == []


async def test_odd_account_ids_are_empty_not_errors(db):
    owner, run, _ = await setup()
    for bad in ("a.b.c.d.e.f.", "nope", "$where"):
        r = await get(owner, run, bad)
        assert (r.status_code, r.json()["jobIds"]) == (200, []), bad
