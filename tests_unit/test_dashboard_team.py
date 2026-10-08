"""GET /dashboard/team: admin-only team totals and per-user rows (mongomock; interviews faked)."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from datetime import date

import pytest

from tests_unit.test_dashboard_compare import TODAY, client_for, db, make_user, plan, tx, weekly  # noqa: F401

pytestmark = pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning")


async def get_team(user, interviews=None, **params):
    client, patcher = client_for(user, interviews)
    with patcher:
        async with client as c:
            return await c.get("/dashboard/team", params={"today": TODAY, **params})


async def test_team_is_admin_only(db):
    pedro = await make_user("Pedro")
    assert (await get_team(pedro)).status_code == 403


async def test_team_rows_and_totals(db):
    root = await make_user("Root", "admin")
    pedro, maria, ana = await make_user("Pedro"), await make_user("Maria"), await make_user("Ana")
    await make_user("Gone", isActive=False)
    await weekly(pedro, date(2026, 10, 5), bids=100, interviews=0)
    await weekly(maria, date(2026, 10, 5), bids=100, interviews=0)
    await plan(pedro, 5, 30, 10)
    await plan(maria, 6, 20, 0)
    await tx(maria, 6, 200.0)
    body = (await get_team(root, interviews={ana.id: 1})).json()
    assert (body["range"], body["from"], body["to"]) == ("week", "2026-10-05", "2026-10-11")
    assert [u["name"] for u in body["users"]] == ["Pedro", "Maria", "Ana"]
    p = body["users"][0]
    assert (p["bids"], p["bidsSelf"], p["bidsBidder"], p["goalBidsPct"], p["goalInterviewsPct"]) == (40, 30, 10, 40, None)
    assert (p["logged"], p["workingDays"]) == (1, 2) and "streak" in p and p["userId"] == str(pedro.id)
    t = body["totals"]
    assert (t["bids"], t["bidsSelf"], t["bidsBidder"], t["interviews"], t["earnings"]) == (60, 50, 10, 1, 200.0)
    assert t["goalBidsPct"] == 30            # 60 done / 200 goal; Ana (no goal) is ignored, not 0%
    assert t["goalInterviewsPct"] is None    # nobody has an interviews goal
    assert (t["logged"], t["workingDays"]) == (2, 6)


async def test_team_bad_range_is_422(db):
    root = await make_user("Root", "admin")
    assert (await get_team(root, range="year")).status_code == 422


async def test_team_shows_real_names_to_admins(db):
    root = await make_user("Root", "admin")
    await make_user("Secret", leaderboardAnon=True)
    assert [u["name"] for u in (await get_team(root)).json()["users"]] == ["Secret"]
