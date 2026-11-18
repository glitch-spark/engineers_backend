"""GET /stats/{tab} route: subject resolution, date validation, tab dispatch (mongomock)."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")
os.environ["RESUME_QUEUE_DISABLED"] = "1"

from datetime import datetime

import pytest
from bson import ObjectId
from beanie import init_beanie
from fastapi import HTTPException
from mongomock_motor import AsyncMongoMockClient

from app.models import Account, Application, DailyPlan, Interview, InterviewQuestion, UserExtra
from app.models.bid import Bid
from app.models.bidder import Bidder
from app.models.weekly_plan import WeeklyPlan
from app.routers import stats
from app.routers.stats import stats_tab

pytestmark = [
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]


@pytest.fixture
async def db():
    database = AsyncMongoMockClient()["stats-router-unit"]
    await init_beanie(database=database, document_models=[
        UserExtra, Account, DailyPlan, Interview, InterviewQuestion, Bid, Bidder, WeeklyPlan, Application])


async def _call(user, tab="bids", **kw):
    args = dict(period="month", date="2026-11-18", to="", region="all", user_id=None)
    return await stats_tab(tab, user, **{**args, **kw})


async def test_interviews_tab_served(db):
    ana = await UserExtra(name="Ana", email="ana@x.io").insert()
    out = await _call(ana, tab="interviews")
    assert out["tab"] == "interviews"


async def test_unknown_tab_422(db):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.deps import get_current_user

    ana = await UserExtra(name="Ana", email="ana@x.io").insert()
    app = FastAPI()
    app.include_router(stats.router)
    app.dependency_overrides[get_current_user] = lambda: ana
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        res = await c.get("/stats/nope")
    assert res.status_code == 422


async def test_staff_sees_self(db):
    ana = await UserExtra(name="Ana", email="ana@x.io").insert()
    assert await _call(ana) is not None
    assert await _call(ana, user_id=str(ana.id)) is not None  # own id is fine


async def test_staff_other_user_403(db):
    ana = await UserExtra(name="Ana", email="ana@x.io").insert()
    ben = await UserExtra(name="Ben", email="ben@x.io").insert()
    with pytest.raises(HTTPException) as e:
        await _call(ana, user_id=str(ben.id))
    assert e.value.status_code == 403


async def test_admin_views_other_user(db, monkeypatch):
    boss = await UserExtra(name="Boss", email="b@x.io", role="admin").insert()
    ben = await UserExtra(name="Ben", email="ben@x.io").insert()
    seen = {}

    async def fake(subject, *a):
        seen["id"] = subject.id
        return {}

    monkeypatch.setitem(stats.VIEWS, "bids", fake)
    await _call(boss, user_id=str(ben.id))
    assert seen["id"] == ben.id
    with pytest.raises(HTTPException) as e:
        await _call(boss, user_id="64b000000000000000000000")
    assert e.value.status_code == 404


async def test_bad_custom_range_400(db):
    ana = await UserExtra(name="Ana", email="ana@x.io").insert()
    with pytest.raises(HTTPException) as e:
        await _call(ana, period="custom", date="2026-11-10", to="")
    assert e.value.status_code == 400


async def test_ai_usage_tab_served(db):
    from app.models import AiUsageEvent
    await init_beanie(database=AsyncMongoMockClient()["stats-router-ai"], document_models=[
        UserExtra, Account, DailyPlan, Interview, InterviewQuestion, Bid, Bidder, WeeklyPlan, AiUsageEvent])
    ana = await UserExtra(name="Ana", email="ana@x.io").insert()
    out = await _call(ana, tab="ai-usage")
    assert out["tab"] == "ai-usage"


# ---- GET /stats/applications/coverage ----

async def _client(user):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.deps import get_current_user

    app = FastAPI()
    app.include_router(stats.router)
    app.dependency_overrides[get_current_user] = lambda: user
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def test_coverage_admin_only(db):
    ana = await UserExtra(name="Ana", email="ana@x.io", role="staff").insert()
    async with await _client(ana) as c:
        assert (await c.get("/stats/applications/coverage")).status_code == 403


async def test_coverage_bad_date_422_and_tabs_still_resolve(db):
    boss = await UserExtra(name="Boss", email="b@x.io", role="admin").insert()
    async with await _client(boss) as c:
        assert (await c.get("/stats/applications/coverage?since=nope")).status_code == 422
        assert (await c.get("/stats/applications/coverage")).status_code == 200
        assert (await c.get("/stats/interviews")).status_code == 200
        assert (await c.get("/stats/nope")).status_code == 422


async def test_coverage_counts(db):
    from datetime import timedelta

    from app.applications import window

    window.clear_cache()
    db = Application.get_motor_collection().database
    boss = await UserExtra(name="Boss", email="b@x.io", role="admin").insert()
    at = datetime(2026, 9, 1, 10)
    iv_ids = []
    for i in range(3):  # iv0 matched by a row; iv1 countable but unmatched; iv2 only canceled rounds
        d = {"_id": ObjectId(), "accountId": ObjectId(), "createdBy": boss.id, "scheduledAt": at + timedelta(days=3), "stage": "intro",
             "status": "canceled" if i == 2 else "completed", "stageHistory": [], "createdAt": at, "updatedAt": at}
        await db["interviews"].insert_one(d)
        iv_ids.append(d["_id"])
    rows = [("a:1", "greenhouse.io", iv_ids[0], at), ("a:2", "greenhouse.io", None, at), ("src:bids:1", None, None, at),
            ("old", "old.io", None, datetime(2026, 7, 1))]
    for k, board, iv, when in rows:
        await db["applications"].insert_one({"key": k, "ownerId": ObjectId(), "appliedAt": when, "board": board,
                                             "interviewId": iv, "sources": []})
    await db["app_settings"].insert_one({"_id": "applications_sync", "checkpoint": at, "lastFull": at})
    async with await _client(boss) as c:
        out = (await c.get("/stats/applications/coverage?since=2026-08-01")).json()
    assert (out["rows"], out["matched"], out["noLink"], out["unmatchedInterviews"]) == (3, 1, 1, 1)
    assert out["byBoard"] == [{"board": "greenhouse.io", "rows": 2}]
    assert out["window"]["thin"] is True
    assert out["lastSync"] == {"checkpoint": "2026-09-01T10:00:00", "lastFull": "2026-09-01T10:00:00"}


async def test_coverage_non_admin_403_before_query_validation(db):
    ana = await UserExtra(name="Ana", email="ana@x.io", role="staff").insert()
    async with await _client(ana) as c:
        assert (await c.get("/stats/applications/coverage?since=nope")).status_code == 403
