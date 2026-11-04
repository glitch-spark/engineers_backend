"""GET /dashboard/compare: bids, interviews and earnings ranked across non-admin users (mongomock)."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from datetime import date, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest
from beanie import init_beanie
from mongomock_motor import AsyncMongoMockClient

from app.models import DailyPlan, Transaction, UserExtra
from app.models.weekly_plan import WeeklyPlan
from app.models.plan_common import Counts, DoneCounts

pytestmark = pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning")

TODAY = "2026-10-07"  # Wednesday; week = Oct 5-11


@pytest.fixture
async def db():
    database = AsyncMongoMockClient()["dashboard-compare"]
    await init_beanie(database=database, document_models=[UserExtra, DailyPlan, Transaction, WeeklyPlan])
    return database


async def make_user(name, role="staff", **kw):
    return await UserExtra(name=name, email=f"{name.lower()}@x.io", role=role, **kw).insert()


async def plan(user, day, self_bids, bidder_bids):
    await DailyPlan(userId=user.id, date=datetime(2026, 10, day), year=2026, weekNumber=40, done=DoneCounts(bidsSelf=self_bids, bidsBidder=bidder_bids),
                    loggedAt=datetime(2026, 10, day, 20)).insert()


async def weekly(user, monday, bids, interviews):
    start = datetime(monday.year, monday.month, monday.day)
    await WeeklyPlan(userId=user.id, weekStart=start, weekNumber=40, year=2026, startDate=start,
                     endDate=start + timedelta(days=6), goal=Counts(bidsSelf=bids, interviewsSelf=interviews)).insert()


async def tx(user, day, amount, status="approved"):
    await Transaction(userId=user.id, date=datetime(2026, 10, day), amount=amount, status=status, payMethod="coin").insert()


def client_for(user, interviews=None):
    """`interviews`: {userId: self rounds, or (self, caller)} held in any window asked for."""
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.deps import get_current_user
    from app.routers import dashboard

    app = FastAPI()
    app.include_router(dashboard.router)
    app.dependency_overrides[get_current_user] = lambda: user
    def split_of(uid):
        n = (interviews or {}).get(uid, 0)
        self_, caller = n if isinstance(n, tuple) else (n, 0)
        return {"self": self_, "caller": caller}

    by_day = AsyncMock(side_effect=lambda ids, start, end: {
        uid: ({start: sum(split_of(uid).values())} if (interviews or {}).get(uid) else {}) for uid in ids
    })
    split = AsyncMock(side_effect=lambda ids, start, end: {uid: split_of(uid) for uid in ids})
    return (AsyncClient(transport=ASGITransport(app=app), base_url="http://test"),
            patch.multiple(dashboard, interviews_by_day=by_day, interviews_split=split))


async def get(user, interviews=None, **params):
    client, patcher = client_for(user, interviews)
    with patcher:
        async with client as c:
            return await c.get("/dashboard/compare", params={"today": TODAY, **params})


async def test_ranks_bids_interviews_and_earnings(db):
    pedro, maria, ana = await make_user("Pedro"), await make_user("Maria"), await make_user("Ana")
    await make_user("Root", "admin")
    await make_user("Gone", isActive=False)
    await plan(pedro, 6, 10, 20)       # 30 this week
    await plan(maria, 5, 50, 0)        # 50
    await plan(maria, 1, 99, 0)        # last week: not counted
    await tx(pedro, 6, 500.0)
    await tx(pedro, 7, -100.0)         # net 400
    await tx(ana, 6, 900.0, status="pending")  # not approved: 0
    r = await get(pedro, interviews={pedro.id: 2, ana.id: (1, 2)})
    assert r.status_code == 200, r.text
    body = r.json()
    assert (body["range"], body["from"], body["to"], body["subjectId"]) == ("week", "2026-10-05", "2026-10-11", str(pedro.id))
    bids = body["metrics"]["bids"]
    assert [(x["name"], x["value"], x["rank"]) for x in bids["rows"]] == [("Maria", 50, 1), ("Pedro", 30, 2), ("Ana", 0, 3)]
    assert bids["subject"] == {"rank": 2, "value": 30, "outOf": 3}
    ivs = body["metrics"]["interviews"]
    assert [(x["name"], x["value"]) for x in ivs["rows"]] == [("Ana", 3), ("Pedro", 2), ("Maria", 0)]
    assert [(x["self"], x["caller"]) for x in ivs["rows"]] == [(1, 2), (2, 0), (0, 0)]
    assert "self" not in bids["rows"][0]
    earn = body["metrics"]["earnings"]
    assert [(x["name"], x["value"]) for x in earn["rows"]][0] == ("Pedro", 400.0)
    assert earn["subject"]["rank"] == 1
    assert all(x["userId"] for m in body["metrics"].values() for x in m["rows"])


async def test_month_and_30_day_ranges(db):
    pedro = await make_user("Pedro")
    await plan(pedro, 1, 5, 0)   # Oct 1: in month and 30d, not this week
    for rng, expected in (("week", 0), ("month", 5), ("30d", 5)):
        body = (await get(pedro, range=rng)).json()
        assert body["metrics"]["bids"]["subject"]["value"] == expected, rng


async def test_admin_views_a_chosen_user_and_is_not_ranked(db):
    root = await make_user("Root", "admin")
    pedro = await make_user("Pedro")
    body = (await get(root, userId=str(pedro.id))).json()
    assert body["subjectId"] == str(pedro.id)
    assert "Root" not in [x["name"] for x in body["metrics"]["bids"]["rows"]]
    assert body["metrics"]["bids"]["subject"]["rank"] == 1


async def test_admin_without_user_has_no_subject_rank(db):
    root = await make_user("Root", "admin")
    await make_user("Pedro")
    body = (await get(root)).json()
    assert body["metrics"]["bids"]["subject"] == {"rank": None, "value": None, "outOf": 1}


async def test_staff_cannot_view_someone_else(db):
    pedro, maria = await make_user("Pedro"), await make_user("Maria")
    assert (await get(pedro, userId=str(maria.id))).status_code == 403


async def test_anonymous_leaderboard_users_stay_anonymous(db):
    pedro = await make_user("Pedro")
    await make_user("Secret", leaderboardAnon=True)
    names = [x["name"] for x in (await get(pedro)).json()["metrics"]["bids"]["rows"]]
    assert "Secret" not in names and any(n.startswith("Anonymous ") for n in names)


async def test_unknown_range_is_422(db):
    pedro = await make_user("Pedro")
    assert (await get(pedro, range="year")).status_code == 422


async def test_compare_has_only_bids_interviews_and_earnings(db):
    pedro = await make_user("Pedro")
    await weekly(pedro, date(2026, 10, 5), bids=60, interviews=4)
    assert set((await get(pedro)).json()["metrics"]) == {"bids", "interviews", "earnings"}


async def test_negative_earnings_rank_below_zero(db):
    pedro = await make_user("Pedro")
    await make_user("Maria")
    await tx(pedro, 6, -50.0)
    rows = (await get(pedro)).json()["metrics"]["earnings"]["rows"]
    assert [(x["name"], x["value"]) for x in rows] == [("Maria", 0), ("Pedro", -50.0)]


async def test_compare_skips_the_streak_scan(db):
    from app.routers import dashboard

    pedro = await make_user("Pedro")
    real, starts = dashboard._logged_plans, []

    async def spy(ids, start, end):
        starts.append(start)
        return await real(ids, start, end)

    with patch.object(dashboard, "_logged_plans", spy):
        assert (await get(pedro, range="30d")).status_code == 200
    assert min(starts) == date(2026, 9, 8)  # never the 400-day streak window
