"""GET /interviews/summary (rounds per status for the Interviews page's filters) and the `region` filter of the
interview feeds (mongomock-motor)."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")
os.environ["RESUME_QUEUE_DISABLED"] = "1"

from datetime import datetime

import pytest
from beanie import PydanticObjectId, init_beanie
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from mongomock_motor import AsyncMongoMockClient

from app.deps import get_current_user
from app.models import Account, Interview, UserExtra
from app.routers import interviews

pytestmark = [
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]

ZERO = {"scheduled": 0, "completed": 0, "passed": 0, "rejected": 0, "canceled": 0}


def rnd(day, status="scheduled", stage="intro", caller=None):
    when = datetime(2026, 10, day, 15)
    return {"stage": stage, "at": when, "scheduledAt": when, "status": status,
            "caller": {"enabled": True, "callerName": caller} if caller else None}


async def interview(owner, account_id, company, *rounds):
    tip = rounds[-1]
    return await Interview(accountId=account_id, createdBy=owner.id, companyName=company, stage=tip["stage"],
                           status=tip["status"], scheduledAt=tip["scheduledAt"], caller=tip["caller"],
                           stageHistory=list(rounds)).insert()


@pytest.fixture
async def world():
    database = AsyncMongoMockClient()["interview-summary-unit"]
    await init_beanie(database=database, document_models=[UserExtra, Account, Interview])
    ana = await UserExtra(name="Ana", email="ana@x.io").insert()
    ben = await UserExtra(name="Ben", email="ben@x.io").insert()
    us = await Account(name="Ana US", createdBy=ana.id, region="US").insert()
    eu = await Account(name="Ana EU", createdBy=ana.id, region="EU").insert()
    bare = await Account(name="Ana none", createdBy=ana.id).insert()
    await interview(ana, us.id, "Acme", rnd(5, "passed"), rnd(7, "rejected", "tech_round_1"))
    await interview(ana, us.id, "Globex", rnd(6, "canceled", caller="Sam"))
    await interview(ana, eu.id, "Initech", rnd(6, "completed"), rnd(20))        # the second round is out of the range
    await interview(ana, bare.id, "Hooli", rnd(8))
    await interview(ana, PydanticObjectId(), "Ghost", rnd(8, "passed"))         # its profile was deleted
    await interview(ben, eu.id, "Bens", rnd(6, "passed"))
    app = FastAPI()
    app.include_router(interviews.router)
    app.dependency_overrides[get_current_user] = lambda: ana
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield {"c": client, "ana": ana, "ben": ben, "us": us, "eu": eu}


RNG = {"from": "2026-10-05", "to": "2026-10-11"}


async def summary(w, **params):
    r = await w["c"].get("/interviews/summary", params={**RNG, **params})
    assert r.status_code == 200, r.text
    return r.json()


async def test_counts_the_rounds_in_range_per_status(world):
    out = await summary(world)
    assert out == {"total": 7, "byStatus": {"scheduled": 1, "completed": 1, "passed": 3, "rejected": 1, "canceled": 1}}
    assert (await summary(world, creatorId=str(world["ana"].id)))["total"] == 6
    assert (await summary(world, **{"from": "", "to": ""}))["total"] == 8      # no dates: every round


async def test_the_status_filter_does_not_narrow_the_breakdown(world):
    assert await summary(world, status="rejected") == await summary(world)
    assert (await world["c"].get("/interviews/summary", params={"status": "nope"})).status_code == 400


async def test_stage_profile_call_type_and_search_narrow_it(world):
    assert (await summary(world, stage="tech_round_1"))["byStatus"] == {**ZERO, "rejected": 1}
    assert (await summary(world, accountId=str(world["us"].id)))["total"] == 3
    assert (await summary(world, callType="caller"))["byStatus"] == {**ZERO, "canceled": 1}
    assert (await summary(world, callType="self"))["total"] == 6
    assert (await summary(world, callerOnly="true"))["total"] == 1
    assert (await summary(world, q="acm"))["byStatus"] == {**ZERO, "passed": 1, "rejected": 1}


async def test_region_narrows_the_summary_and_the_feeds(world):
    assert (await summary(world, region="US"))["total"] == 3
    assert (await summary(world, region="EU"))["byStatus"] == {**ZERO, "completed": 1, "passed": 1}
    # no region: a profile without one, and an interview whose profile is gone
    assert (await summary(world, region="none"))["byStatus"] == {**ZERO, "scheduled": 1, "passed": 1}
    assert (await summary(world, region="US", accountId=str(world["eu"].id)))["total"] == 0
    assert (await world["c"].get("/interviews/summary", params={"region": "Mars"})).status_code == 400

    c = world["c"]
    listed = (await c.get("/interviews", params={**RNG, "region": "EU"})).json()
    assert sorted(iv["companyName"] for iv in listed["interviews"]) == ["Bens", "Initech"]
    assert listed["pagination"]["total"] == 2
    cal = (await c.get("/interviews/rounds", params={"from": "2026-10-05T00:00:00Z", "to": "2026-10-12T00:00:00Z",
                                                     "region": "none"})).json()
    assert sorted(r["companyName"] for r in cal["rounds"]) == ["Ghost", "Hooli"]
    callers = (await c.get("/interviews/caller-rounds", params={**RNG, "region": "US"})).json()
    assert [r["companyName"] for r in callers["rounds"]] == ["Globex"]
    assert (await c.get("/interviews/caller-rounds", params={**RNG, "region": "EU"})).json()["rounds"] == []
