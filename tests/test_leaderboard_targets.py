"""Leaderboard weekly targets come from the weekly plan's Goal."""

from datetime import datetime, timezone

from app.models.plan_common import monday_of


async def test_leaderboard_targets_from_goal_and_done(client, staff_user, auth_header):
    monday = monday_of(datetime.now(timezone.utc).date()).isoformat()
    h = auth_header(staff_user)
    r = await client.put(
        f"/weekly-plans/{monday}",
        headers=h,
        json={"goal": {"bidsSelf": 30, "bidsBidder": 20, "interviewsSelf": 4, "interviewsCaller": 1}},
    )
    assert r.status_code == 200, r.text

    r = await client.get("/metrics/leaderboard/consolidated?range=week", headers=h)
    assert r.status_code == 200, r.text
    row = next(u for u in r.json()["users"] if u["userId"] == str(staff_user.id))
    assert row["bidsTarget"] == 50
    assert row["bidsPlan"] == 50
    assert row["interviewsTarget"] == 5


async def test_leaderboard_without_weekly_plan_has_zero_targets(client, staff_user, auth_header):
    r = await client.get("/metrics/leaderboard/consolidated?range=week", headers=auth_header(staff_user))
    assert r.status_code == 200
    row = next(u for u in r.json()["users"] if u["userId"] == str(staff_user.id))
    assert row["bidsTarget"] == 0 and row["interviewsTarget"] == 0
