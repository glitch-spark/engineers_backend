from datetime import date, datetime

import pytest
from beanie import PydanticObjectId

from app.alerts import app_week_key
from app.config import get_settings
from app.models import DailyPlan, Interview, Transaction, WeeklyPlan
from app.models.interview import InterviewCaller, InterviewStageEvent
from app.models.plan_common import Counts, DoneOverrides


async def _plan(user, day: date, self_=0, bidder=0, iv=0, plan_type="regular"):
    """A daily plan in the Goal vs Done shape. `plan_type="custom"` stands for
    an entry that was never logged (goal only), which the dashboard ignores;
    `iv` is stored as the day's interviews Done override."""
    dt = datetime(day.year, day.month, day.day)
    year, week_number, _ = app_week_key(dt)
    logged = plan_type == "regular"
    plan = DailyPlan(
        userId=user.id,
        date=dt,
        year=year,
        weekNumber=week_number,
        # A never-logged entry only has a goal: Done and overrides are saved when logging.
        goal=Counts(bidsSelf=self_, bidsBidder=bidder, interviewsSelf=iv) if not logged else Counts(),
        done=Counts(bidsSelf=self_, bidsBidder=bidder) if logged else Counts(),
        doneOverrides=DoneOverrides(interviewsSelf=iv) if logged and iv else DoneOverrides(),
        loggedAt=dt if logged else None,
    )
    await plan.insert()
    return plan


async def _weekly_plan(user, monday: date, metrics):
    dt = datetime(monday.year, monday.month, monday.day)
    year, week_number, _ = app_week_key(dt)
    return await _weekly_plan_raw(
        user, year=year, week_number=week_number, start_date=dt, end_date=dt, metrics=metrics
    )


async def _weekly_plan_raw(user, *, year, week_number, start_date, end_date, metrics):
    """A weekly plan stored in the pre Goal-vs-Done shape (metrics targets), so
    these tests also cover the legacy fold: bids/interviews targets become
    goal.bidsSelf / goal.interviewsSelf. Explicit year/weekNumber/startDate let
    tests build the creation-date-vs-Monday mismatch around year boundaries."""
    await WeeklyPlan.get_motor_collection().insert_one({
        "userId": user.id,
        "year": year,
        "weekNumber": week_number,
        "startDate": start_date,
        "endDate": end_date,
        "metrics": metrics,
        "createdAt": start_date,
        "updatedAt": start_date,
    })


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


async def test_week_interviews_percentile_excludes_deleted_users(
    client, staff_user, staff_user_2, admin_user, auth_header, make_user
):
    # staff A 3 interviews, staff B 1, staff C 0 this week (regular plans).
    staff_c = await make_user(name="Staff Three", email="staff3@example.com", role="staff")
    await _plan(staff_user, date(2026, 9, 28), iv=3)
    await _plan(staff_user_2, date(2026, 9, 28), iv=1)
    await _plan(staff_c, date(2026, 9, 28), iv=0)

    # A regular plan owned by a user id that no longer exists (deleted user)
    # must not enlarge the population N.
    dt = datetime(2026, 9, 28)
    year, week_number, _ = app_week_key(dt)
    deleted_plan = DailyPlan(
        userId=PydanticObjectId(),
        date=dt,
        year=year,
        weekNumber=week_number,
        planType="regular",
        bidsHandsOn=0,
        bidsByBidder=0,
        interviewsDone=5,
    )
    await deleted_plan.insert()

    # A gets top with N still 3 (percent 34), not diluted to N=4 (percent 25)
    # by the deleted user's plan.
    r = await client.get("/dashboard/week?today=2026-10-01", headers=auth_header(staff_user))
    assert r.status_code == 200
    assert r.json()["interviews"]["percentile"] == {"position": "top", "percent": 34}

    # C's zero score never reads as a percentile, regardless of rank/ties.
    r = await client.get("/dashboard/week?today=2026-10-01", headers=auth_header(staff_c))
    assert r.status_code == 200
    assert r.json()["interviews"]["percentile"] is None


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


async def test_activity_week_days_with_sunday_merged(client, staff_user, auth_header):
    # plans Mon Sep 28 (2/1/0), Sat Oct 3 (1/0/1), Sun Oct 4 (1/1/0)
    await _plan(staff_user, date(2026, 9, 28), self_=2, bidder=1, iv=0)
    await _plan(staff_user, date(2026, 10, 3), self_=1, bidder=0, iv=1)
    await _plan(staff_user, date(2026, 10, 4), self_=1, bidder=1, iv=0)

    h = auth_header(staff_user)
    r = await client.get("/dashboard/activity?from=2026-09-28&to=2026-10-04", headers=h)
    assert r.status_code == 200
    b = r.json()
    assert b["bucket"] == "day" and len(b["series"]) == 6
    assert b["series"][0] == {"key": "2026-09-28", "label": "Mon", "self": 2, "bidder": 1, "interviews": 0}
    assert b["series"][5] == {"key": "2026-10-03", "label": "Sat", "self": 2, "bidder": 1, "interviews": 1}
    assert b["totals"] == {"self": 4, "bidder": 2, "bids": 6, "interviews": 1}


async def test_activity_year_is_monthly_and_zero_filled(client, staff_user, auth_header):
    h = auth_header(staff_user)
    r = await client.get("/dashboard/activity?from=2026-01-01&to=2026-12-31", headers=h)
    assert r.status_code == 200
    b = r.json()
    assert b["bucket"] == "month" and len(b["series"]) == 12 and b["series"][0]["label"] == "Jan"


async def test_activity_ignores_custom_plans_and_other_users(client, staff_user, staff_user_2, auth_header):
    await _plan(staff_user, date(2026, 9, 28), self_=3, bidder=1, iv=1)
    await _plan(staff_user, date(2026, 9, 29), self_=9, bidder=9, iv=9, plan_type="custom")
    await _plan(staff_user_2, date(2026, 9, 28), self_=5, bidder=5, iv=5)

    r = await client.get(
        "/dashboard/activity?from=2026-09-28&to=2026-10-03", headers=auth_header(staff_user)
    )
    assert r.status_code == 200
    b = r.json()
    assert b["totals"] == {"self": 3, "bidder": 1, "bids": 4, "interviews": 1}


@pytest.mark.parametrize(
    "qs",
    [
        "from=2026-10-05&to=2026-10-01",
        "from=2020-01-01&to=2026-01-01",
        "from=2026-13-01&to=2026-12-01",
        "to=2026-10-01",
    ],
)
async def test_activity_bad_ranges_422(client, staff_user, auth_header, qs):
    assert (await client.get(f"/dashboard/activity?{qs}", headers=auth_header(staff_user))).status_code == 422


async def test_net_monthly_approved_owner_only_twelve_months(client, staff_user, staff_user_2, auth_header):
    async def _tx(user, day, amount, status_, payer=None):
        tx = Transaction(
            userId=user.id,
            date=datetime(day.year, day.month, day.day),
            amount=amount,
            status=status_,
            payerId=payer.id if payer else None,
        )
        await tx.insert()
        return tx

    # staff owns:
    await _tx(staff_user, date(2026, 9, 3), 3000, "approved")
    await _tx(staff_user, date(2026, 9, 10), -900, "approved")
    await _tx(staff_user, date(2026, 9, 5), 500, "pending")
    await _tx(staff_user, date(2026, 8, 2), 700, "rejected")
    await _tx(staff_user, date(2026, 3, 1), -200, "approved")
    await _tx(staff_user, date(2025, 9, 20), 100, "approved")  # outside 12-month window
    # staff_user_2 owns an approved row payerId=staff — must not count for staff
    await _tx(staff_user_2, date(2026, 9, 4), 999, "approved", payer=staff_user)

    r = await client.get("/dashboard/net-monthly?today=2026-09-15", headers=auth_header(staff_user))
    assert r.status_code == 200
    b = r.json()
    assert len(b["months"]) == 12 and b["months"][0]["period"] == "2025-10" and b["months"][-1]["period"] == "2026-09"
    assert b["months"][-1] == {"period": "2026-09", "income": 3000, "outcome": 900, "net": 2100}
    mar = next(m for m in b["months"] if m["period"] == "2026-03")
    assert mar["net"] == -200
    assert next(m for m in b["months"] if m["period"] == "2026-08")["net"] == 0
    assert b["total"] == 1900


async def test_net_monthly_new_user_zeros(client, staff_user, auth_header):
    r = await client.get("/dashboard/net-monthly?today=2026-09-15", headers=auth_header(staff_user))
    assert r.status_code == 200
    b = r.json()
    assert all(m["net"] == 0 for m in b["months"]) and b["total"] == 0


async def test_net_monthly_admin_user_id(client, staff_user, admin_user, auth_header):
    tx = Transaction(
        userId=staff_user.id,
        date=datetime(2026, 9, 3),
        amount=500,
        status="approved",
    )
    await tx.insert()

    r = await client.get(
        f"/dashboard/net-monthly?userId={staff_user.id}&today=2026-09-15",
        headers=auth_header(admin_user),
    )
    assert r.status_code == 200
    assert r.json()["months"][-1]["income"] == 500

    assert (
        await client.get(
            f"/dashboard/net-monthly?userId={staff_user.id}&today=2026-09-15",
            headers=auth_header(staff_user),
        )
    ).status_code == 403


async def test_net_monthly_naive_month_boundaries_regardless_of_app_timezone(
    client, staff_user, auth_header, monkeypatch
):
    # Transaction.date is a calendar date stored as naive midnight (no tz
    # meaning) — month boundaries/grouping must not convert it through
    # APP_TIMEZONE, or Sep 1 drifts into "2026-08" under America/New_York
    # and the 1st of the oldest month can fall outside the match window.
    monkeypatch.setattr(get_settings(), "APP_TIMEZONE", "America/New_York")

    async def _tx(day, amount):
        tx = Transaction(
            userId=staff_user.id,
            date=datetime(day.year, day.month, day.day),
            amount=amount,
            status="approved",
        )
        await tx.insert()

    await _tx(date(2026, 9, 1), 3000)
    await _tx(date(2025, 10, 1), 100)  # the 1st of the oldest month in the window

    r = await client.get("/dashboard/net-monthly?today=2026-09-15", headers=auth_header(staff_user))
    assert r.status_code == 200
    b = r.json()
    assert b["months"][0]["period"] == "2025-10" and b["months"][0]["net"] == 100
    assert b["months"][-1]["period"] == "2026-09" and b["months"][-1]["net"] == 3000


async def test_week_counts_unmigrated_legacy_daily_plans(client, staff_user, auth_header):
    """Deployed before the migration runs: old-shape regular plans still count."""
    await DailyPlan.get_motor_collection().insert_one({
        "userId": staff_user.id, "date": datetime(2026, 9, 29), "year": 2026, "weekNumber": 40,
        "planType": "regular", "bidsHandsOn": 5, "bidsByBidder": 2, "interviewsDone": 1,
        "createdAt": datetime(2026, 9, 29, 21), "updatedAt": datetime(2026, 9, 29, 21),
    })
    await Interview(accountId=PydanticObjectId(), createdBy=staff_user.id, scheduledAt=datetime(2026, 9, 29, 15),
                    stage="intro", status="scheduled",
                    caller=InterviewCaller(enabled=True, startsAt=datetime(2026, 9, 29, 15))).insert()

    b = (await client.get("/dashboard/week?today=2026-09-30", headers=auth_header(staff_user))).json()
    assert b["bids"]["self"] == 5 and b["bids"]["bidder"] == 2
    # The old count was the whole day: no automatic caller round on top.
    assert b["interviews"]["done"] == 1
    assert b["streak"] == 1

    a = (await client.get("/dashboard/activity?from=2026-09-28&to=2026-10-04", headers=auth_header(staff_user))).json()
    assert a["totals"]["self"] == 5 and a["totals"]["interviews"] == 1
