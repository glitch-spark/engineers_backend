"""GET /stats/applications/changes and /stats/applications/leaders (mongomock)."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")
os.environ["RESUME_QUEUE_DISABLED"] = "1"

from datetime import datetime, timedelta, timezone

import pytest
from beanie import init_beanie
from bson import ObjectId
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from mongomock_motor import AsyncMongoMockClient

from app import interview_rates
from app.dashboard_stats import app_tz
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
Q = f"?period=custom&date={(NOW - timedelta(days=200)).date()}&to={(NOW + timedelta(days=1)).date()}"
WIN = {"median": 5, "p90": 14, "matured_days": 14, "n": 40, "thin": False}


@pytest.fixture(autouse=True)
def _clear_loader_cache():
    profile_loader.clear_cache()
    yield
    profile_loader.clear_cache()


@pytest.fixture
async def db(monkeypatch):
    await init_beanie(database=AsyncMongoMockClient()["stats-changes-unit"], document_models=[
        UserExtra, Account, DailyPlan, Interview, InterviewQuestion, Bid, Bidder, WeeklyPlan, Application])

    async def regions():
        return {}, {}

    async def window(today):
        return WIN

    monkeypatch.setattr(interview_rates, "_profile_regions", regions)
    monkeypatch.setattr(stats, "response_window", window)


def _client(user):
    app = FastAPI()
    app.include_router(stats.router)
    app.dependency_overrides[get_current_user] = lambda: user
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _acct(owner, name, region="EU"):
    return await Account(name=name, region=region, createdBy=owner.id, template="x").insert()


async def _rows(acct, owner, n, iv=0, days_ago=60, region="EU", **kw):
    await Application.get_motor_collection().insert_many([{
        "key": f"{acct.id}:{ObjectId()}", "accountId": acct.id, "ownerId": owner.id, "region": region,
        "appliedAt": NOW - timedelta(days=days_ago), "interviewId": ObjectId() if i < iv else None,
        "daysToInterview": 5 if i < iv else None, "furthest": "intro" if i < iv else None, "by": "self",
        "syncedAt": NOW, **kw} for i in range(n)])


async def _users():
    ana = await UserExtra(name="Ana", email="ana@x.io").insert()
    ben = await UserExtra(name="Ben", email="ben@x.io").insert()
    return ana, ben


async def _get(user, path="changes", qs=Q):
    async with _client(user) as c:
        return await c.get(f"/stats/applications/{path}" + qs)


async def test_changes_shape(db):
    ana, ben = await _users()
    dead, fine = await _acct(ana, "Dead"), await _acct(ana, "Fine")
    await _rows(dead, ana, 40)
    await _rows(fine, ana, 40, iv=4, furthest="tech")
    for name in ("B1", "B2"):
        await _rows(await _acct(ben, name), ben, 40, iv=4)
    res = await _get(ana)
    assert res.status_code == 200, res.text
    out = res.json()
    assert out["subject"] == {"userId": str(ana.id), "name": "Ana"} and out["label"]
    assert out["profiles"] == [{"accountId": str(dead.id), "name": "Dead", "grade": "dead", "suggestions": [{
        "pattern": "dead", "evidence": {"matured": 40, "maturedDays": 14},
        "text": "Archive it, or rebuild it with a new base resume / prompt and judge it fresh after 14 days."}]}]


async def test_changes_skips_archived_profiles(db):
    ana, ben = await _users()
    gone = await _acct(ana, "Gone")
    await Account.get_motor_collection().update_one({"_id": gone.id}, {"$set": {"archived": True}})
    await _rows(gone, ana, 40)
    for name in ("B1", "B2"):
        await _rows(await _acct(ben, name), ben, 40, iv=4)
    assert (await _get(ana)).json()["profiles"] == []


async def test_changes_subject_rule(db):
    ana, ben = await _users()
    boss = await UserExtra(name="Boss", email="b@x.io", role="admin").insert()
    await _rows(await _acct(ben, "Bens"), ben, 3)
    assert (await _get(ana, qs=Q + f"&user={ben.id}")).status_code == 403
    assert (await _get(ana, qs=Q + f"&user={ana.id}")).status_code == 200
    assert (await _get(boss, qs=Q + f"&user={ben.id}")).json()["subject"]["name"] == "Ben"


async def test_changes_names_top_bidder(db):
    ana, _ = await _users()
    acct = await _acct(ana, "Mixed")
    bob = await Bidder(createdBy=ana.id, name="Bob", rate=1, screenshotFolderUrl="x").insert()
    await _rows(acct, ana, 40, iv=4, furthest="tech")
    await _rows(acct, ana, 40, by="bidder", bidderId=bob.id)
    sug = (await _get(ana)).json()["profiles"][0]["suggestions"]
    assert [s["pattern"] for s in sug] == ["bidder_gap"]
    assert sug[0]["text"].startswith("Your own applies convert better than your bidders' (4 of 40 vs 0 of 40 matured")
    assert "review Bob's jobs" in sug[0]["text"]


async def test_changes_volume_up_uses_previous_period(db):
    ana, ben = await _users()
    acct = await _acct(ana, "Busy")
    await _rows(acct, ana, 150, iv=15, days_ago=45)  # previous 30 days: 10.0 per 100
    await _rows(acct, ana, 200, iv=8, days_ago=20)   # current: 4.0 per 100
    for name in ("B1", "B2"):
        await _rows(await _acct(ben, name), ben, 40, iv=1, days_ago=20)  # team median 2.5
    qs = f"?period=custom&date={(NOW - timedelta(days=29)).date()}&to={NOW.date()}"
    sug = (await _get(ana, qs=qs)).json()["profiles"][0]["suggestions"]
    assert "volume_up_rate_down" in [s["pattern"] for s in sug]


async def _quiet_profile(ana):
    acct = await _acct(ana, "Quiet")
    await _rows(acct, ana, 1, days_ago=60)
    await _rows(acct, ana, 1, days_ago=4)
    return acct


async def test_quiet_now_counts_unmatched_interviews(db):
    ana, _ = await _users()
    acct = await _quiet_profile(ana)
    out = (await _get(ana)).json()
    assert [s["pattern"] for s in out["profiles"][0]["suggestions"]] == ["quiet_now"]
    at = NOW - timedelta(days=10)  # an interview no row carries
    await Interview.get_motor_collection().insert_one({
        "accountId": acct.id, "createdBy": ana.id, "scheduledAt": at, "stage": "intro", "status": "completed",
        "stageHistory": [{"stage": "intro", "at": at, "scheduledAt": at}], "createdAt": at, "updatedAt": at})
    profile_loader.clear_cache()
    assert (await _get(ana)).json()["profiles"] == []


EDGE = datetime(2026, 12, 1, 3)  # UTC Dec 1, still Nov 30 in the app timezone


async def test_quiet_now_fires_in_month_view(db, monkeypatch):
    class Clock(datetime):  # the router's clock, frozen at the month edge
        @classmethod
        def utcnow(cls):
            return EDGE

        @classmethod
        def now(cls, tz=None):
            return EDGE.replace(tzinfo=timezone.utc).astimezone(tz) if tz else EDGE

    monkeypatch.setattr(stats, "datetime", Clock)
    today = Clock.now(app_tz()).date()
    assert today != EDGE.date()  # pins the UTC-vs-app-timezone edge
    ana, _ = await _users()
    acct = await _acct(ana, "Old")
    await Application(key=f"{acct.id}:edge", accountId=acct.id, ownerId=ana.id, region="EU", appliedAt=EDGE,
                      profileAgeDays=120, syncedAt=EDGE).insert()  # profile ~4 months old, bidding today
    qs = f"?period=month&date={today}"
    sug = (await _get(ana, qs=qs)).json()["profiles"][0]["suggestions"]
    assert [s["pattern"] for s in sug] == ["quiet_now"]


async def test_quiet_now_only_when_period_includes_today(db):
    ana, _ = await _users()
    await _quiet_profile(ana)
    qs = f"?period=custom&date={(NOW - timedelta(days=200)).date()}&to={(NOW - timedelta(days=3)).date()}"
    assert (await _get(ana, qs=qs)).json()["profiles"] == []


async def test_leaders_visible_to_staff_with_names(db):
    ana, ben = await _users()
    for i, (owner, region) in enumerate([(ana, "EU"), (ben, "EU"), (ana, "EU"), (ben, "US"), (ana, "EU")]):
        a = await _acct(owner, f"P{i}", region)
        await _rows(a, owner, 150, iv=i, region=region)
    await _rows(await _acct(ana, "Small"), ana, 149, iv=50)
    res = await _get(ana, "leaders", Q + f"&user={ben.id}")  # ignores user, any signed-in user
    assert res.status_code == 200, res.text
    out = res.json()
    assert out["note"] == "These are patterns, not proof." and out["window"] == WIN and out["label"]
    assert [p["name"] for p in out["best"]] == ["P4", "P3", "P2"]
    assert [p["name"] for p in out["weakest"]] == ["P0", "P1"]
    assert out["best"][1]["owner"] == "Ben" and out["best"][1]["region"] == "US"
    assert out["groups"]["best"]["matured"] == 450
    eu = (await _get(ana, "leaders", Q + "&region=EU")).json()
    assert [p["name"] for p in eu["best"]] == ["P4", "P2", "P1"]
