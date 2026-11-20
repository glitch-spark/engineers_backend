"""GET /stats/conversion `summary`: top cards, Everyone and coverage (mongomock)."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")
os.environ["RESUME_QUEUE_DISABLED"] = "1"

from datetime import datetime

import pytest
from beanie import init_beanie
from bson import ObjectId
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from mongomock_motor import AsyncMongoMockClient

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
Q = "?period=custom&date=2026-11-02&to=2026-11-15"


def _d(day, hour=15):
    return datetime(2026, 11, day, hour)


@pytest.fixture
async def db(monkeypatch):
    profile_loader.clear_cache()
    await init_beanie(database=AsyncMongoMockClient()["conversion-summary-unit"], document_models=[
        UserExtra, Account, DailyPlan, Interview, InterviewQuestion, Bid, Bidder, WeeklyPlan, Application])

    async def window(today):
        return {"median": 5, "p90": 14, "matured_days": 14, "n": 40, "thin": False}

    monkeypatch.setattr(stats, "response_window", window)
    yield
    profile_loader.clear_cache()


async def _user(name, role="staff"):
    return await UserExtra(name=name, email=f"{name.lower()}@x.io", role=role).insert()


async def _acct(owner, name, region="EU", archived=False):
    return await Account(name=name, region=region, createdBy=owner.id, template="x", archived=archived).insert()


async def _plan(user, day, **done):
    await DailyPlan(userId=user.id, date=datetime(2026, 11, day), year=2026, weekNumber=45, done=done).insert()


async def _bids(user, n, eu=None, us=0, day=3):
    await _plan(user, day, bidsSelf=n, bidsSelfEu=n - us if eu is None else eu, bidsSelfUs=us)


async def _iv(acct, owner, first, rounds=(), **kw):
    """An interview created by `owner` on `acct`: history rounds (stage, at) then the tip round at `first`."""
    doc = {"accountId": acct.id, "createdBy": owner.id, "scheduledAt": first, "stage": "intro", "status": "completed",
           "createdAt": first, "updatedAt": first, **kw}
    # history holds every round, the tip included (the model's legacy resolver rebuilds the tip from it)
    trail = [*rounds, (doc["stage"], first)] if rounds else []
    doc["stageHistory"] = [{"stage": s, "status": "completed", "at": at, "scheduledAt": at} for s, at in trail]
    return (await Interview.get_motor_collection().insert_one(doc)).inserted_id


async def _rows(acct, owner, n, iv=0, at=_d(4)):
    await Application.get_motor_collection().insert_many([{
        "key": f"{acct.id}:{ObjectId()}", "accountId": acct.id, "ownerId": owner.id, "region": acct.region,
        "appliedAt": at, "interviewId": ObjectId() if i < iv else None, "by": "self", "syncedAt": at}
        for i in range(n)])


async def _get(user, qs=Q):
    app = FastAPI()
    app.include_router(stats.router)
    app.dependency_overrides[get_current_user] = lambda: user
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        r = await c.get("/stats/conversion" + qs)
        assert r.status_code == 200, r.text
        return r.json()


async def _team():
    """Ana 40 bids / 2 started (4 rounds held); Ben 10 / 2; Cy 60 / 0; Dee 0 / 1; Eve nothing; an admin."""
    ana, ben, cy, dee, eve = [await _user(n) for n in ("Ana", "Ben", "Cy", "Dee", "Eve")]
    boss = await _user("Boss", role="admin")
    a, b, c, d = [await _acct(u, u.name) for u in (ana, ben, cy, dee)]
    await _bids(ana, 40)
    await _bids(ben, 10)
    await _bids(cy, 60)
    await _bids(boss, 99)
    await _iv(a, ana, _d(10), rounds=[("intro", _d(5))], stage="tech_round_1")    # started Nov 5, 2 rounds held
    await _iv(a, ana, _d(6))                                               # started, 1 round
    await _iv(a, ana, _d(7), rounds=[("intro", datetime(2026, 10, 20))], stage="tech_round_1")  # started before: 1 round only
    await _iv(a, ana, _d(8), status="canceled")                            # canceled only: nothing
    await _iv(a, ana, _d(8), stage="ai_interview")                         # AI interview: nothing
    await _iv(a, ben, _d(9))                                               # on Ana's profile, created by Ben: Ben's
    await _iv(b, ben, _d(11))
    await _iv(d, dee, _d(12))
    await _iv(d, boss, _d(12))                                             # admins are not on the team
    await _rows(a, ana, 30, iv=2)
    await _rows(b, ben, 8)
    await _rows(c, cy, 50)
    return ana, ben, cy, dee, eve


def _row(out, name):
    return next(r for r in out["summary"]["everyone"] if r["name"] == name)


async def test_me_scope_cards_rank_the_subject(db):
    ana, *_ = await _team()
    s = (await _get(ana))["summary"]
    # population: members with >= 1 bid = Ana 40, Ben 10, Cy 60
    assert s["cards"]["bids"] == {"value": 40, "self": 40, "bidder": 0, "rank": 2, "of": 3, "median": 40,
                                  "vsMedian": 0.0}
    assert s["cards"]["interviews"] == {"value": 2, "rounds": 4, "rank": 1, "of": 3, "median": 2, "vsMedian": 0.0}
    # per 100 ranked among >= 30 bids (Ana 5.0, Cy 0.0); team rate = 5 started / 110 bids
    assert s["cards"]["per100"] == {"value": 5.0, "rank": 1, "of": 2, "teamRate": 4.5}


async def test_team_scope_cards_are_totals_without_rank(db):
    ana, *_ = await _team()
    s = (await _get(ana, Q + "&scope=team"))["summary"]
    assert s["cards"]["bids"] == {"value": 110, "self": 110, "bidder": 0, "rank": None, "of": None, "median": 40,
                                  "vsMedian": None}
    assert s["cards"]["interviews"] == {"value": 5, "rounds": 7, "rank": None, "of": None, "median": 2,
                                        "vsMedian": None}
    assert s["cards"]["per100"] == {"value": 4.5, "rank": None, "of": None, "teamRate": 4.5}
    assert [r["name"] for r in s["everyone"]] == [r["name"] for r in (await _get(ana))["summary"]["everyone"]]


async def test_everyone_rows_and_per100_needs_30_bids(db):
    await _team()
    out = await _get(await UserExtra.find_one(UserExtra.name == "Ana"))
    names = [r["name"] for r in out["summary"]["everyone"]]
    assert sorted(names) == ["Ana", "Ben", "Cy", "Dee"]  # Eve has nothing, Boss is an admin
    ana, ben, cy, dee = (_row(out, n) for n in ("Ana", "Ben", "Cy", "Dee"))
    assert ana["isSubject"] and not ben["isSubject"]
    assert (ana["bids"], ana["interviews"], ana["per100"], ana["ranks"]) == (
        40, 2, 5.0, {"bids": 2, "interviews": 1, "per100": 1})
    # interviews but < 30 bids: ranked on bids and interviews, not on per 100
    assert (ben["per100"], ben["ranks"]) == (None, {"bids": 3, "interviews": 1, "per100": None})
    # bids but 0 interviews: a real 0 per 100, ranked
    assert (cy["per100"], cy["ranks"]) == (0.0, {"bids": 1, "interviews": 3, "per100": 2})
    # interviews with no bids: listed, ranked where they'd fall, never on per 100
    assert (dee["bids"], dee["interviews"], dee["per100"], dee["ranks"]["per100"]) == (0, 1, None, None)
    assert dee["ranks"]["interviews"] == 3


async def test_coverage_reconciles(db):
    ana, *_ = await _team()
    me = (await _get(ana))["summary"]["coverage"]
    assert me == {"rows": 30, "bids": 40, "matched": 2, "interviews": 2}
    team = (await _get(ana, Q + "&scope=team"))["summary"]["coverage"]
    assert team == {"rows": 88, "bids": 110, "matched": 2, "interviews": 5}
    for c in (me, team):
        assert c["rows"] <= c["bids"] and c["matched"] <= c["interviews"]


async def test_region_filter_applies_to_bids_interviews_and_everyone(db):
    ana, ben = await _user("Ana"), await _user("Ben")
    eu, us = await _acct(ana, "AnaEu"), await _acct(ana, "AnaUs", region="US")
    gone = await _acct(ana, "AnaOld", archived=True)                    # archived EU profile: still counted
    ben_us = await _acct(ben, "BenUs", region="US")
    await _bids(ana, 50, us=20)
    await _bids(ben, 30, us=30)
    await _iv(eu, ana, _d(5))
    await _iv(gone, ana, _d(6))
    await _iv(us, ana, _d(7))
    await _iv(ben_us, ben, _d(8))
    await _rows(eu, ana, 25, iv=1)
    await _rows(us, ana, 10)
    s = (await _get(ana, Q + "&region=EU"))["summary"]
    assert (s["cards"]["bids"]["value"], s["cards"]["interviews"]["value"]) == (30, 2)
    assert [r["name"] for r in s["everyone"]] == ["Ana"]                 # Ben has nothing in EU
    assert s["coverage"] == {"rows": 25, "bids": 30, "matched": 1, "interviews": 2}
    s = (await _get(ana, Q + "&region=US"))["summary"]
    assert (s["cards"]["bids"]["value"], s["cards"]["interviews"]["value"]) == (20, 1)
    assert sorted(r["name"] for r in s["everyone"]) == ["Ana", "Ben"]
    s = (await _get(ana))["summary"]
    assert (s["cards"]["bids"]["value"], s["cards"]["interviews"]["value"]) == (50, 3)


async def test_subject_with_nothing_still_gets_cards(db):
    ana, *_ = await _team()
    eve = await UserExtra.find_one(UserExtra.name == "Eve")
    s = (await _get(eve))["summary"]
    assert s["cards"]["bids"]["value"] == 0 and s["cards"]["bids"]["rank"] == 4 and s["cards"]["bids"]["of"] == 4
    assert s["cards"]["per100"] == {"value": None, "rank": None, "of": 2, "teamRate": 4.5}
    assert "Eve" not in [r["name"] for r in s["everyone"]]
