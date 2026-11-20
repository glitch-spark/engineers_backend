"""conversion_scope (me / profile / team, active profiles, current region, from DATA_START) and the stats cards
that use it (mongomock)."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")
os.environ["RESUME_QUEUE_DISABLED"] = "1"

from datetime import date, datetime

import pytest
from beanie import init_beanie
from bson import ObjectId
from fastapi import FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient
from mongomock_motor import AsyncMongoMockClient

from app import interview_rates
from app.dashboard_stats import app_tz, local_day_start_utc
from app.deps import get_current_user
from app.models import Account, Application, DailyPlan, Interview, InterviewQuestion, UserExtra
from app.models.bid import Bid
from app.models.bidder import Bidder
from app.models.weekly_plan import WeeklyPlan
from app.routers import stats
from app.stats import conversion_scope as cs
from app.stats import profile_loader

pytestmark = [
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]
WIN = {"median": 5, "p90": 14, "matured_days": 14, "n": 40, "thin": False}
OCT10 = datetime(2026, 10, 10, 15)
SEP20 = datetime(2026, 9, 20, 15)
Q = "?period=custom&date=2026-10-07&to=2026-11-18"


@pytest.fixture(autouse=True)
def _clear_loader_cache():
    profile_loader.clear_cache()
    yield
    profile_loader.clear_cache()


@pytest.fixture
async def db(monkeypatch):
    await init_beanie(database=AsyncMongoMockClient()["conversion-scope-unit"], document_models=[
        UserExtra, Account, DailyPlan, Interview, InterviewQuestion, Bid, Bidder, WeeklyPlan, Application])

    async def regions():
        return {}, {}

    async def counts(user_ids, *a, **k):
        return {u: (0, 0, 0) for u in user_ids}

    async def window(today):
        return WIN

    monkeypatch.setattr(interview_rates, "_profile_regions", regions)
    monkeypatch.setattr(interview_rates, "counts", counts)
    monkeypatch.setattr(stats, "response_window", window)


async def _users():
    ana = await UserExtra(name="Ana", email="ana@x.io").insert()
    ben = await UserExtra(name="Ben", email="ben@x.io").insert()
    boss = await UserExtra(name="Boss", email="b@x.io", role="admin").insert()
    return ana, ben, boss


async def _acct(owner, name, region="EU", archived=False):
    return await Account(name=name, region=region, archived=archived, createdBy=owner.id, template="x").insert()


async def _rows(acct, owner, n, iv=0, at=OCT10, region=None, **kw):
    await Application.get_motor_collection().insert_many([{
        "key": f"{acct.id}:{ObjectId()}", "accountId": acct.id, "ownerId": owner.id, "region": region or acct.region,
        "appliedAt": at, "interviewId": ObjectId() if i < iv else None, "daysToInterview": 5 if i < iv else None,
        "furthest": "intro" if i < iv else None, "by": "self", "syncedAt": at, **kw} for i in range(n)])


def _accts(rows):
    return {str(r["accountId"]) for r in rows}


async def test_clamp_to_oct6(db):
    ana, _, _ = await _users()
    a = await _acct(ana, "Alpha")
    await _rows(a, ana, 2, at=SEP20)
    await _rows(a, ana, 3)
    sc = await cs.conversion_scope(ana, "custom", "2026-09-01", "2026-10-20", "all", None)
    assert sc["startClamped"] is True and len(sc["rows"]) == 3
    assert sc["start"] == local_day_start_utc(date(2026, 10, 6), app_tz())
    assert sc["label"] == "Oct 6–20, 2026"
    before = await cs.conversion_scope(ana, "custom", "2026-09-01", "2026-09-30", "all", None)
    assert before["startClamped"] is True and before["rows"] == [] and before["teamRows"] == []
    after = await cs.conversion_scope(ana, "custom", "2026-10-07", "2026-10-20", "all", None)
    assert after["startClamped"] is False and len(after["rows"]) == 3


async def test_scope_uses_current_region(db):
    ana, ben, _ = await _users()
    moved = await _acct(ana, "Moved", "EU")  # rows say US: the profile's region changed since
    us = await _acct(ana, "Us", "US")
    gone = await _acct(ana, "Gone", "EU", archived=True)
    team_eu = await _acct(ben, "BenEu", "EU")
    team_gone = await _acct(ben, "BenGone", "EU", archived=True)
    await _rows(moved, ana, 3, region="US")
    for acct, owner in ((us, ana), (gone, ana), (team_eu, ben), (team_gone, ben)):
        await _rows(acct, owner, 2)
    sc = await cs.conversion_scope(ana, "custom", "2026-10-07", "2026-10-20", "EU", None)
    assert _accts(sc["rows"]) == {str(moved.id), str(gone.id)} and len(sc["rows"]) == 5
    assert _accts(sc["teamRows"]) == {str(moved.id), str(gone.id), str(team_eu.id), str(team_gone.id)}
    assert sc["accounts"][str(moved.id)] == {"name": "Moved", "region": "EU", "archived": False, "ownerId": ana.id}
    assert sc["accounts"][str(gone.id)]["archived"] is True
    every = await cs.conversion_scope(ana, "custom", "2026-10-07", "2026-10-20", "all", None)
    assert _accts(every["rows"]) == {str(moved.id), str(us.id), str(gone.id)}
    team = await cs.conversion_scope(ana, "custom", "2026-10-07", "2026-10-20", "EU", None, scope="team")
    assert team["rows"] == team["teamRows"] and set(team["accounts"]) == {
        str(moved.id), str(gone.id), str(team_eu.id), str(team_gone.id)}


async def test_archived_profiles_count_in_every_scope(db):
    ana, ben, _ = await _users()
    gone = await _acct(ana, "Gone", "EU", archived=True)
    await _rows(gone, ana, 2)
    for kw in ({}, {"scope": "team"}, {"scope": "profile", "profile": str(gone.id)}):
        sc = await cs.conversion_scope(ana, "custom", "2026-10-07", "2026-10-20", "EU", None, **kw)
        assert len(sc["rows"]) == 2 and str(gone.id) in sc["accounts"]


async def test_profile_scope_other_users_profile_403(db):
    ana, ben, boss = await _users()
    mine, other = await _acct(ana, "Mine"), await _acct(ana, "Other")
    bens = await _acct(ben, "Bens")
    for acct, owner in ((mine, ana), (other, ana), (bens, ben)):
        await _rows(acct, owner, 2)
    args = ("custom", "2026-10-07", "2026-10-20", "all", None)
    sc = await cs.conversion_scope(ana, *args, scope="profile", profile=str(mine.id))
    assert _accts(sc["rows"]) == {str(mine.id)} and len(sc["teamRows"]) == 6
    with pytest.raises(HTTPException) as e:
        await cs.conversion_scope(ana, *args, scope="profile", profile=str(bens.id))
    assert e.value.status_code == 403
    with pytest.raises(HTTPException) as e:
        await cs.conversion_scope(ana, *args, scope="profile", profile=str(ObjectId()))
    assert e.value.status_code == 404
    with pytest.raises(HTTPException) as e:
        await cs.conversion_scope(ana, *args, scope="profile")
    assert e.value.status_code == 422
    admin = await cs.conversion_scope(boss, *args, scope="profile", profile=str(bens.id))
    assert _accts(admin["rows"]) == {str(bens.id)}


# ---- endpoints ----
def _client(user):
    app = FastAPI()
    app.include_router(stats.router)
    app.dependency_overrides[get_current_user] = lambda: user
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _get(user, path, qs=Q):
    async with _client(user) as c:
        return await c.get(f"/stats/applications/{path}" + qs)


async def test_team_rows_are_the_baseline(db):
    ana, ben, _ = await _users()
    a = await _acct(ana, "Alpha")
    await _rows(a, ana, 40, iv=4)                                          # 4 / 40
    await _rows(await _acct(ben, "BenEu"), ben, 40, iv=8)                  # 8 / 40
    await _rows(await _acct(ben, "Gone", archived=True), ben, 40, iv=40)   # archived: in the baseline
    await _rows(await _acct(ben, "BenUs", "US"), ben, 40, iv=40)           # other region: out
    await _rows(a, ana, 10, iv=10, at=SEP20)                               # before DATA_START: out
    out = (await _get(ana, "profiles", Q + "&region=EU")).json()
    assert out["team"]["per100"] == 43.3  # 52 of 120
    assert [p["name"] for p in out["profiles"]] == ["Alpha"] and out["profiles"][0]["per100"] == 10.0


async def test_profiles_scope_profile_and_team(db):
    ana, ben, _ = await _users()
    a, b = await _acct(ana, "Alpha"), await _acct(ana, "Beta")
    for acct in (a, b):
        await _rows(acct, ana, 5)
    await _rows(await _acct(ben, "BenEu"), ben, 5)
    one = (await _get(ana, "profiles", Q + f"&scope=profile&profile={b.id}")).json()
    assert [p["name"] for p in one["profiles"]] == ["Beta"]
    team = (await _get(ana, "profiles", Q + "&scope=team")).json()
    assert team["profiles"] == [] and team["team"]["profiles"] == 0
    assert (await _get(ana, "changes", Q + "&scope=team")).json()["profiles"] == []
    assert (await _get(ana, "profiles", Q + f"&scope=profile&profile={ObjectId()}")).status_code == 404


async def test_timing_scopes_you_and_team(db):
    ana, ben, _ = await _users()
    bob = await Bidder(createdBy=ana.id, name="Bob", rate=1, screenshotFolderUrl="u").insert()
    await _rows(await _acct(ana, "Alpha"), ana, 5, by="bidder", bidderId=bob.id)
    await _rows(await _acct(ben, "BenEu"), ben, 7)
    me = (await _get(ana, "timing")).json()
    assert "bidders" not in me  # bidder quality removed (user, 2026-11-19)
    assert sum(g["you"]["bids"] for g in me["weekday"]) == 5 and sum(g["team"]["bids"] for g in me["weekday"]) == 12
    team = (await _get(ana, "timing", Q + "&scope=team")).json()
    assert sum(g["you"]["bids"] for g in team["weekday"]) == 12 == sum(g["team"]["bids"] for g in team["weekday"])


async def _volume_setup(ana, ben):
    acct = await _acct(ana, "Busy")
    await _rows(acct, ana, 150, iv=15, at=datetime(2026, 10, 10, 15))  # previous window: 10.0 per 100
    await _rows(acct, ana, 200, iv=8, at=datetime(2026, 10, 25, 15))   # current: 4.0 per 100
    for name in ("B1", "B2"):
        await _rows(await _acct(ben, name), ben, 40, iv=1, at=datetime(2026, 10, 25, 15))


async def test_volume_suggestion_off_before_data_start(db, monkeypatch):
    ana, ben, _ = await _users()
    await _volume_setup(ana, ben)
    qs = "?period=custom&date=2026-10-15&to=2026-11-10"  # previous window Sep 18 - Oct 14 starts before Oct 6

    async def patterns():
        profile_loader.clear_cache()
        return [s["pattern"] for s in (await _get(ana, "changes", qs)).json()["profiles"][0]["suggestions"]]

    assert "volume_up_rate_down" not in await patterns()
    monkeypatch.setattr(cs, "DATA_START", date(2026, 1, 1))
    assert "volume_up_rate_down" in await patterns()
