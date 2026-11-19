"""GET /stats/applications/profiles: report shape, coverage, region, subject rules (mongomock)."""

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
    await init_beanie(database=AsyncMongoMockClient()["stats-profiles-unit"], document_models=[
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
        return await c.get("/stats/applications/profiles" + qs)


async def test_profiles_report_shape(db):
    ana, ben = await _users()
    a, b = await _acct(ana, "Alpha"), await _acct(ana, "Beta")
    other = await _acct(ben, "Other")
    await _rows(a, ana, 40, iv=8, furthest="tech")  # 20 per 100, reaches tech -> strong
    await _rows(b, ana, 40, iv=2)                    # 5 per 100 -> watch
    await _rows(other, ben, 40, iv=4)                # 10 per 100 (team rows of another owner)
    res = await _get(ana)
    assert res.status_code == 200, res.text
    out = res.json()
    assert [p["name"] for p in out["profiles"]] == ["Alpha", "Beta"]
    assert [p["grade"] for p in out["profiles"]] == ["strong", "watch"]
    assert out["team"]["per100"] == 10.0 and out["team"]["profiles"] == 3
    assert out["subject"] == {"userId": str(ana.id), "name": "Ana"}
    assert out["coverage"] == {"rows": 80, "headerBids": 40, "noLink": 0, "unmatchedInterviews": 0}
    assert out["profiles"][1]["reasons"] and out["profiles"][0]["per100"] == 20.0


async def test_coverage_excludes_rejected_and_counts_no_link(db):
    ana, _ = await _users()
    a = await _acct(ana, "Alpha")
    await _rows(a, ana, 5)
    await _rows(a, ana, 3, status="rejected")
    await _rows(a, ana, 2, src=True)
    out = (await _get(ana)).json()
    assert out["coverage"]["rows"] == 7 and out["coverage"]["noLink"] == 2
    assert out["profiles"][0]["bidsSelf"] == 7


async def test_region_scopes_profiles_and_team(db):
    ana, ben = await _users()
    eu, us = await _acct(ana, "Eu", "EU"), await _acct(ana, "Us", "US")
    other = await _acct(ben, "Other", "US")
    await _rows(eu, ana, 35, iv=7, region="EU")
    await _rows(us, ana, 35, iv=1, region="US")
    await _rows(other, ben, 35, iv=3, region="US")
    out = (await _get(ana, Q + "&region=EU")).json()
    assert [p["name"] for p in out["profiles"]] == ["Eu"]
    assert out["team"]["profiles"] == 1 and out["team"]["per100"] == 20.0
    assert out["coverage"]["rows"] == 35


async def test_user_param_rules(db):
    ana, ben = await _users()
    boss = await UserExtra(name="Boss", email="b@x.io", role="admin").insert()
    await _rows(await _acct(ben, "Bens"), ben, 3)
    assert (await _get(ana, Q + f"&user={ben.id}")).status_code == 403
    assert (await _get(ana, Q + f"&user={ana.id}")).status_code == 200  # own id means self, as /stats/{tab}
    out = (await _get(boss, Q + f"&user={ben.id}")).json()
    assert [p["name"] for p in out["profiles"]] == ["Bens"] and out["subject"]["name"] == "Ben"


async def test_no_rows_empty_report(db):
    ana, _ = await _users()
    await _acct(ana, "Idle")
    out = (await _get(ana)).json()
    assert out["profiles"] == [] and out["coverage"]["rows"] == 0


async def test_route_does_not_shadow_stats_tab(db):
    ana, _ = await _users()
    async with _client(ana) as c:
        assert (await c.get("/stats/bids")).status_code == 200


async def test_sorting_by_grade_then_per100_then_name(db):
    ana, _ = await _users()
    for name, iv in (("Zed", 10), ("Amy", 10), ("Low", 4)):
        await _rows(await _acct(ana, name), ana, 40, iv=iv, furthest="tech")
    young = await _acct(ana, "Young")
    await _rows(young, ana, 3, days_ago=1)
    out = (await _get(ana)).json()
    assert [p["name"] for p in out["profiles"]] == ["Amy", "Zed", "Low", "Young"]
    assert out["profiles"][-1]["grade"] == "early"


async def test_unmatched_interviews_respect_region(db, monkeypatch):
    ana, _ = await _users()
    eu, us = await _acct(ana, "Eu", "EU"), await _acct(ana, "Us", "US")

    async def regions():
        return {eu.id: "EU", us.id: "US"}, 0

    monkeypatch.setattr(interview_rates, "_profile_regions", regions)
    at = NOW - timedelta(days=10)
    for acc in (eu, us, us):  # countable interviews no row carries
        await Interview.get_motor_collection().insert_one({
            "accountId": acc.id, "createdBy": ana.id, "scheduledAt": at, "stage": "intro", "status": "completed",
            "stageHistory": [], "createdAt": at, "updatedAt": at})
    assert (await _get(ana, Q + "&region=EU")).json()["coverage"]["unmatchedInterviews"] == 1
    assert (await _get(ana, Q + "&region=US")).json()["coverage"]["unmatchedInterviews"] == 2
    assert (await _get(ana)).json()["coverage"]["unmatchedInterviews"] == 3


def test_application_has_applied_at_index():
    keys = [list(i.document["key"].items()) for i in Application.Settings.indexes]
    assert [("appliedAt", 1)] in keys
