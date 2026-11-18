"""GET /stats/bids route: subject resolution + date validation (mongomock)."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")
os.environ["RESUME_QUEUE_DISABLED"] = "1"

import pytest
from beanie import init_beanie
from fastapi import HTTPException
from mongomock_motor import AsyncMongoMockClient

from app.models import Account, DailyPlan, Interview, InterviewQuestion, UserExtra
from app.models.bid import Bid
from app.models.bidder import Bidder
from app.models.weekly_plan import WeeklyPlan
from app.routers.stats import stats_bids

pytestmark = [
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]


@pytest.fixture
async def db():
    database = AsyncMongoMockClient()["stats-router-unit"]
    await init_beanie(database=database, document_models=[
        UserExtra, Account, DailyPlan, Interview, InterviewQuestion, Bid, Bidder, WeeklyPlan])


async def _call(user, **kw):
    args = dict(period="month", date="2026-11-18", to="", region="all", user_id=None)
    return await stats_bids(user, **{**args, **kw})


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

    monkeypatch.setattr("app.routers.stats.bids_view", fake)
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
