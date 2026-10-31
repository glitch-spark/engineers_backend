"""Self / Caller: the Interviews list filters on the latest round's caller, the calendar on each round's, and the
dashboard splits interviews held into self and caller rounds (mongomock-motor)."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")
os.environ["RESUME_QUEUE_DISABLED"] = "1"

from datetime import date, datetime
from unittest.mock import AsyncMock, patch

import pytest
from beanie import init_beanie
from mongomock_motor import AsyncMongoMockClient

from app import plan_auto
from app.models import Account, Interview, UserExtra
from app.routers.interviews import flatten_rounds

pytestmark = [
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]


@pytest.fixture
async def db():
    database = AsyncMongoMockClient()["call-type-unit"]
    await init_beanie(database=database, document_models=[UserExtra, Account, Interview])
    return database


def _round(day, caller, stage="intro"):
    when = datetime(2026, 10, day, 15)
    return {"stage": stage, "at": when, "scheduledAt": when, "status": "scheduled",
            "caller": {"enabled": True, "callerName": "Muhammad"} if caller else None}


async def _interview(owner, acc, company, *rounds):
    tip = rounds[-1]
    return await Interview(accountId=acc.id, createdBy=owner.id, companyName=company, stage=tip["stage"],
                           scheduledAt=tip["scheduledAt"], caller=tip["caller"], stageHistory=list(rounds)).insert()


async def test_list_and_calendar_filter_self_and_caller(db):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.deps import get_current_user
    from app.routers import interviews

    ana = await UserExtra(name="Ana", email="ana@x.io").insert()
    acc = await Account(name="Ana US", createdBy=ana.id).insert()
    await _interview(ana, acc, "Selfie", _round(5, caller=False))
    await _interview(ana, acc, "Callco", _round(6, caller=True))
    # Caller first, self now: the list goes by the latest round, the calendar by each round.
    await _interview(ana, acc, "Mixed", _round(7, caller=True), _round(8, caller=False, stage="tech_round_1"))

    app = FastAPI()
    app.include_router(interviews.router)
    app.dependency_overrides[get_current_user] = lambda: ana
    rng = {"from": "2026-10-05T00:00:00Z", "to": "2026-10-11T23:59:59Z"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        async def companies(**params):
            r = await c.get("/interviews", params={**rng, **params})
            assert r.status_code == 200, r.text
            return sorted(iv["companyName"] for iv in r.json()["interviews"])

        async def rounds(**params):
            r = await c.get("/interviews/rounds", params={"from": "2026-10-05T00:00:00Z", "to": "2026-10-12T00:00:00Z",
                                                          **params})
            assert r.status_code == 200, r.text
            return sorted((x["companyName"], x["stage"]) for x in r.json()["rounds"])

        assert await companies() == ["Callco", "Mixed", "Selfie"]
        assert await companies(callType="self") == ["Mixed", "Selfie"]
        assert await companies(callType="caller") == ["Callco"]
        assert (await c.get("/interviews", params={"callType": "both"})).status_code == 400

        assert await rounds(callType="self") == [("Mixed", "tech_round_1"), ("Selfie", "intro")]
        assert await rounds(callType="caller") == [("Callco", "intro"), ("Mixed", "intro")]


def test_flatten_rounds_by_call_type():
    iv = {"_id": "iv1", "companyName": "Acme", "stageHistory": [
        {"id": "s", "stage": "intro", "status": "scheduled", "scheduledAt": datetime(2026, 10, 5, 15)},
        {"id": "c", "stage": "intro", "status": "scheduled", "scheduledAt": datetime(2026, 10, 6, 15),
         "caller": {"enabled": True}},
    ]}
    window = {"from_dt": datetime(2026, 10, 5), "to_dt": datetime(2026, 10, 12)}
    assert [r["roundId"] for r in flatten_rounds([iv], **window, call_type="self")] == ["s"]
    assert [r["roundId"] for r in flatten_rounds([iv], **window, call_type="caller")] == ["c"]
    assert [r["roundId"] for r in flatten_rounds([iv], **window)] == ["s", "c"]


async def test_interviews_split_adds_up_each_day():
    counts = {"u1": {date(2026, 10, 5): {"self": 2, "caller": 1}, date(2026, 10, 6): {"self": 0, "caller": 3}}}
    with patch.object(plan_auto, "interview_counts", AsyncMock(return_value=counts)):
        split = await plan_auto.interviews_split(["u1", "u2"], date(2026, 10, 5), date(2026, 10, 11))
    assert split == {"u1": {"self": 2, "caller": 4}, "u2": {"self": 0, "caller": 0}}
