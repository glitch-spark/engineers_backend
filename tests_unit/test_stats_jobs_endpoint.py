"""GET /stats/applications/jobs: shape, subject rules, region scoping (mongomock)."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")
os.environ["RESUME_QUEUE_DISABLED"] = "1"

from datetime import datetime, timedelta

import pytest
from beanie import init_beanie
from bson import ObjectId
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from mongomock_motor import AsyncMongoMockClient

from app import interview_rates
from app.applications import window as window_mod
from app.deps import get_current_user
from app.models import Account, Application, DailyPlan, Interview, InterviewQuestion, UserExtra
from app.models.bid import Bid
from app.models.bidder import Bidder
from app.models.weekly_plan import WeeklyPlan
from app.routers import stats
from app.stats import profile_loader

pytestmark = [
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]
NOW = datetime.utcnow()
Q = f"?period=custom&date={(NOW - timedelta(days=200)).date()}&to={(NOW + timedelta(days=1)).date()}"  # covers every fixture row


@pytest.fixture(autouse=True)
def _clear_loader_cache():
    profile_loader.clear_cache()
    yield
    profile_loader.clear_cache()


@pytest.fixture
async def db(monkeypatch):
    await init_beanie(database=AsyncMongoMockClient()["stats-jobs-unit"], document_models=[
        UserExtra, Account, DailyPlan, Interview, InterviewQuestion, Bid, Bidder, WeeklyPlan, Application])
    window_mod.clear_cache()

    async def regions():
        return {}, {}

    async def counts(user_ids, *a, **k):
        return {u: (40, 0, 0) for u in user_ids}

    monkeypatch.setattr(interview_rates, "_profile_regions", regions)
    monkeypatch.setattr(interview_rates, "counts", counts)


def _client(user):
    app = FastAPI()
    app.include_router(stats.router)
    app.dependency_overrides[get_current_user] = lambda: user
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _acct(owner, name, region="EU", archived=False):
    return await Account(name=name, region=region, archived=archived, createdBy=owner.id, template="x").insert()


async def _rows(acct, owner, n, iv=0, days_ago=60, region="EU", status=None, src=False, furthest="intro"):
    for i in range(n):
        has = i < iv
        await Application(
            key=f"src:bids:{acct.id}{i}{days_ago}" if src else f"{acct.id}:{i}:{days_ago}:{status}", accountId=acct.id,
            ownerId=owner.id, region=region, appliedAt=NOW - timedelta(days=days_ago), bidStatus=status,
            interviewId=ObjectId() if has else None, daysToInterview=5 if has else None,
            furthest=furthest if has else None, firstRoundAt=NOW - timedelta(days=days_ago - 5) if has else None,
            syncedAt=NOW).insert()


async def _users():
    ana = await UserExtra(name="Ana", email="ana@x.io").insert()
    ben = await UserExtra(name="Ben", email="ben@x.io").insert()
    return ana, ben


async def _get(user, qs=Q):
    async with _client(user) as c:
        return await c.get("/stats/applications/jobs" + qs)


async def _app_rows(acct, owner, n, iv=0, region="EU", **kw):
    for i in range(n):
        await Application(
            key=f"{acct.id}:{i}:{kw.get('board')}", accountId=acct.id, ownerId=owner.id, region=region,
            appliedAt=NOW - timedelta(days=60), interviewId=ObjectId() if i < iv else None, syncedAt=NOW, **kw).insert()


async def test_jobs_shape(db):
    ana, ben = await _users()
    mine, other = await _acct(ana, "Mine"), await _acct(ben, "Other")
    await _app_rows(mine, ana, 40, iv=4, board="lever.co", area="Backend", scoreBand="good", profileAgeDays=20)
    await _app_rows(other, ben, 40, iv=2, board="lever.co", area="Backend", profileAgeDays=100)
    out = (await _get(ana)).json()
    assert out["subject"] == {"userId": str(ana.id), "name": "Ana"} and out["window"] and out["label"]
    lever = out["bySite"][0]
    assert lever["label"] == "Lever"
    assert lever["you"] == {"bids": 40, "matured": 40, "interviews": 4, "per100": 10.0, "tooFew": False}
    assert lever["team"]["bids"] == 80 and lever["team"]["per100"] == 7.5
    assert out["known"] == {"score": 100, "area": 100}
    assert [x["label"] for x in out["byAge"]][:2] == ["Under 2 weeks", "2–6 weeks"]
    assert out["byArea"][0]["label"] == "Backend"


async def test_jobs_user_param_rules(db):
    ana, ben = await _users()
    boss = await UserExtra(name="Boss", email="b@x.io", role="admin").insert()
    await _app_rows(await _acct(ben, "Bens"), ben, 3, board="lever.co")
    assert (await _get(ana, Q + f"&user={ben.id}")).status_code == 403
    assert (await _get(ana, Q + f"&user={ana.id}")).status_code == 200
    out = (await _get(boss, Q + f"&user={ben.id}")).json()
    assert out["subject"]["name"] == "Ben" and out["bySite"][0]["you"]["bids"] == 3


async def test_jobs_region_scopes_team(db):
    ana, ben = await _users()
    eu, other = await _acct(ana, "Eu", "EU"), await _acct(ben, "Other", "US")
    await _app_rows(eu, ana, 35, iv=7, region="EU", board="lever.co")
    await _app_rows(other, ben, 35, iv=1, region="US", board="lever.co")
    out = (await _get(ana, Q + "&region=EU")).json()
    assert out["bySite"][0]["team"]["bids"] == 35 and out["bySite"][0]["team"]["per100"] == 20.0
    assert (await _get(ana)).json()["bySite"][0]["team"]["bids"] == 70
