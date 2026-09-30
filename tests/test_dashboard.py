from datetime import date, datetime

import pytest
from beanie import PydanticObjectId

from app.alerts import app_week_key
from app.models import DailyPlan, Interview, WeeklyPlan
from app.models.interview import InterviewStageEvent


async def _plan(user, day: date, self_=0, bidder=0, iv=0, plan_type="regular"):
    dt = datetime(day.year, day.month, day.day)
    year, week_number, _ = app_week_key(dt)
    plan = DailyPlan(
        userId=user.id,
        date=dt,
        year=year,
        weekNumber=week_number,
        planType=plan_type,
        bidsHandsOn=self_,
        bidsByBidder=bidder,
        interviewsDone=iv,
    )
    await plan.insert()
    return plan


async def _weekly_plan(user, monday: date, metrics):
    dt = datetime(monday.year, monday.month, monday.day)
    year, week_number, _ = app_week_key(dt)
    plan = WeeklyPlan(
        userId=user.id,
        year=year,
        weekNumber=week_number,
        startDate=dt,
        endDate=dt,
        metrics=metrics,
    )
    await plan.insert()
    return plan


async def _weekly_plan_raw(user, *, year, week_number, start_date, end_date, metrics):
    """Like `_weekly_plan` but with explicit year/weekNumber/startDate/endDate,
    so tests can construct the creation-date-vs-Monday mismatch that happens
    around year boundaries (see test_week_plan_lookup_falls_back_across_year_boundary)."""
    plan = WeeklyPlan(
        userId=user.id,
        year=year,
        weekNumber=week_number,
        startDate=start_date,
        endDate=end_date,
        metrics=metrics,
    )
    await plan.insert()
    return plan


async def test_week_requires_auth(client):
    assert (await client.get("/dashboard/week")).status_code == 401


async def test_week_new_user_is_all_zero(client, staff_user, auth_header):
    r = await client.get("/dashboard/week?today=2026-10-01", headers=auth_header(staff_user))
    assert r.status_code == 200
    b = r.json()
    assert b["week"] == {"start": "2026-09-28", "end": "2026-10-03"}
    assert b["bids"] == {"self": 0, "bidder": 0, "total": 0, "target": None, "pace": None, "percentile": None}
    assert b["interviews"]["done"] == 0 and b["streak"] == 0 and b["hasWeeklyPlan"] is False
    assert [s["key"] for s in b["stages"]] == ["intro", "tech", "hiring", "panel", "final", "canceled"]
    assert all(s["count"] == 0 for s in b["stages"])


async def test_week_sums_regular_plans_only_and_sunday_rolls_back(client, staff_user, auth_header):
    # Mon 4/2/1, Wed 3/1/0, Sun Oct 4 1/1/1, custom plan Tue 9/9/9, last Sat Sep 26 5/5/5
    await _plan(staff_user, date(2026, 9, 28), self_=4, bidder=2, iv=1)
    await _plan(staff_user, date(2026, 9, 30), self_=3, bidder=1, iv=0)
    await _plan(staff_user, date(2026, 10, 4), self_=1, bidder=1, iv=1)
    await _plan(staff_user, date(2026, 9, 29), self_=9, bidder=9, iv=9, plan_type="custom")
    await _plan(staff_user, date(2026, 9, 26), self_=5, bidder=5, iv=5)

    r = await client.get("/dashboard/week?today=2026-10-01", headers=auth_header(staff_user))
    assert r.status_code == 200
    b = r.json()
    assert b["bids"]["self"] == 8 and b["bids"]["bidder"] == 4 and b["bids"]["total"] == 12
    assert b["interviews"]["done"] == 2


async def test_week_targets_and_pace_from_weekly_plan(client, staff_user, auth_header):
    # WeeklyPlan metrics bids target 50, interviews target 0 (→ None); plans total 20 bids
    await _weekly_plan(
        staff_user,
        date(2026, 9, 28),
        [
            {"key": "bids", "label": "Job applies", "target": 50},
            {"key": "interviews", "label": "Interviews", "target": 0},
        ],
    )
    await _plan(staff_user, date(2026, 9, 28), self_=20, bidder=0)

    r = await client.get("/dashboard/week?today=2026-10-01", headers=auth_header(staff_user))
    assert r.status_code == 200
    b = r.json()
    assert b["bids"]["target"] == 50 and b["hasWeeklyPlan"] is True
    assert b["bids"]["pace"] == {"expected": 33, "behindBy": 13, "perDayNeeded": 15, "onTrack": False}
    assert b["interviews"]["target"] is None and b["interviews"]["pace"] is None


async def test_week_plan_without_core_metrics_has_no_target(client, staff_user, auth_header):
    # WeeklyPlan with metrics=[{"key": "study", "label": "Study", "target": 5}]
    await _weekly_plan(
        staff_user,
        date(2026, 9, 28),
        [{"key": "study", "label": "Study", "target": 5}],
    )

    r = await client.get("/dashboard/week?today=2026-10-01", headers=auth_header(staff_user))
    assert r.status_code == 200
    b = r.json()
    assert b["bids"]["target"] is None and b["hasWeeklyPlan"] is True


async def test_week_stage_tiles_and_canceled(client, staff_user, auth_header):
    # interviews createdBy staff: intro Mon + tech_round_1 Wed (stageHistory), final Sat 23:30 UTC,
    # rejected+scheduled Tue, ai_interview Thu, intro on next Mon Oct 5 (outside)
    account_id = PydanticObjectId()

    def _iv(**kw):
        return Interview(accountId=account_id, createdBy=staff_user.id, **kw)

    await _iv(
        scheduledAt=datetime(2026, 9, 30, 10),
        stage="tech_round_1",
        status="scheduled",
        stageHistory=[
            InterviewStageEvent(
                stage="intro", at=datetime(2026, 9, 28, 9), scheduledAt=datetime(2026, 9, 28, 9)
            ),
            InterviewStageEvent(
                stage="tech_round_1", at=datetime(2026, 9, 30, 10), scheduledAt=datetime(2026, 9, 30, 10)
            ),
        ],
    ).insert()
    await _iv(scheduledAt=datetime(2026, 10, 3, 23, 30), stage="final", status="scheduled").insert()
    await _iv(scheduledAt=datetime(2026, 9, 29, 12), stage="rejected", status="scheduled").insert()
    await _iv(scheduledAt=datetime(2026, 10, 1, 9), stage="ai_interview", status="scheduled").insert()
    await _iv(scheduledAt=datetime(2026, 10, 5, 9), stage="intro", status="scheduled").insert()

    r = await client.get("/dashboard/week?today=2026-10-01", headers=auth_header(staff_user))
    assert r.status_code == 200
    b = r.json()
    counts = {s["key"]: s["count"] for s in b["stages"]}
    assert counts == {"intro": 1, "tech": 1, "hiring": 0, "panel": 0, "final": 1, "canceled": 1}


async def test_week_streak(client, staff_user, auth_header):
    # regular plans Thu Oct 1, Wed, Tue, Mon, Sat Sep 26 (Sun skipped), gap on Fri Sep 25
    for day in (date(2026, 10, 1), date(2026, 9, 30), date(2026, 9, 29), date(2026, 9, 28), date(2026, 9, 26)):
        await _plan(staff_user, day)

    r = await client.get("/dashboard/week?today=2026-10-01", headers=auth_header(staff_user))
    assert r.status_code == 200
    assert r.json()["streak"] == 5


async def test_week_percentile_excludes_admins_and_needs_three(
    client, staff_user, staff_user_2, admin_user, auth_header, make_user
):
    # staff A 30 bids, staff B 20, staff C 10 this week; admin with a plan is ignored
    staff_c = await make_user(name="Staff Three", email="staff3@example.com", role="staff")
    await _plan(staff_user, date(2026, 9, 28), self_=30)
    await _plan(staff_user_2, date(2026, 9, 28), self_=20)
    plan_c = await _plan(staff_c, date(2026, 9, 28), self_=10)
    await _plan(admin_user, date(2026, 9, 28), self_=999)

    # as C → {"position": "bottom", "percent": 34}
    r = await client.get("/dashboard/week?today=2026-10-01", headers=auth_header(staff_c))
    assert r.status_code == 200
    assert r.json()["bids"]["percentile"] == {"position": "bottom", "percent": 34}

    # as A → {"position": "top", "percent": 34}
    r = await client.get("/dashboard/week?today=2026-10-01", headers=auth_header(staff_user))
    assert r.status_code == 200
    assert r.json()["bids"]["percentile"] == {"position": "top", "percent": 34}

    # with only A and B active → percentile None
    await plan_c.delete()
    r = await client.get("/dashboard/week?today=2026-10-01", headers=auth_header(staff_user))
    assert r.status_code == 200
    assert r.json()["bids"]["percentile"] is None


async def test_week_user_id_param(client, staff_user, staff_user_2, admin_user, auth_header):
    assert (
        await client.get(f"/dashboard/week?userId={staff_user_2.id}", headers=auth_header(staff_user))
    ).status_code == 403

    # Seed a regular plan for staff_user_2 only — the admin's response should
    # reflect staff_user_2's data, not staff_user's (nothing seeded for staff_user).
    await _plan(staff_user_2, date(2026, 9, 28), self_=7)
    r = await client.get(
        f"/dashboard/week?userId={staff_user_2.id}&today=2026-10-01", headers=auth_header(admin_user)
    )
    assert r.status_code == 200
    assert r.json()["bids"]["self"] == 7

    assert (
        await client.get(
            "/dashboard/week?userId=000000000000000000000000", headers=auth_header(admin_user)
        )
    ).status_code == 404
    assert (
        await client.get("/dashboard/week?userId=nope", headers=auth_header(admin_user))
    ).status_code == 400


async def test_week_bad_today_is_422(client, staff_user, auth_header):
    assert (
        await client.get("/dashboard/week?today=garbage", headers=auth_header(staff_user))
    ).status_code == 422


@pytest.mark.parametrize("bad_today", ["20261001", "9999-12-31", "0001-01-01"])
async def test_week_malformed_today_is_422_not_500(client, staff_user, auth_header, bad_today):
    r = await client.get(f"/dashboard/week?today={bad_today}", headers=auth_header(staff_user))
    assert r.status_code == 422


async def test_week_plan_lookup_falls_back_across_year_boundary(client, staff_user, auth_header):
    # Frontend stores year/weekNumber from the plan's *creation* date (Sunday-start
    # week numbers), so a plan created Thu 2026-01-01 is stored (year=2026, weekNumber=1)
    # even though its Monday (startDate) is 2025-12-29 — app_week_key(monday) alone
    # would ask for (2025, 53) and miss it. The startDate window lookup must find it.
    await _weekly_plan_raw(
        staff_user,
        year=2026,
        week_number=1,
        start_date=datetime(2025, 12, 29, 5, 0),
        end_date=datetime(2026, 1, 4, 23, 59),
        metrics=[{"key": "bids", "label": "Job applies", "target": 40}],
    )

    r = await client.get("/dashboard/week?today=2026-01-01", headers=auth_header(staff_user))
    assert r.status_code == 200
    b = r.json()
    assert b["bids"]["target"] == 40
    assert b["hasWeeklyPlan"] is True
