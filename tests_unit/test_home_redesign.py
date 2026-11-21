"""Home redesign reads: the day view's `todayInterviews` / `interviewsMarked`, the week's `bids.byDay`,
`interviews.outcomes` and `routines.rows`, net-monthly `in` / `out` and GET /dashboard/month-interviews (mongomock).
docs/superpowers/specs/2026-11-20-home-redesign-design.md"""
import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")
os.environ["RESUME_QUEUE_DISABLED"] = "1"

from datetime import date, datetime, time, timedelta, timezone

import pytest
from beanie import PydanticObjectId, init_beanie
from fastapi import HTTPException
from mongomock_motor import AsyncMongoMockClient

from app import plan_bidder
from app.dashboard_stats import app_tz
from app.models import Account, Application, Transaction, UserExtra
from app.models.bid import Bid
from app.models.bidder import Bidder
from app.models.caller import Caller
from app.models.daily_plan import DailyPlan
from app.models.interview import Interview, InterviewStageEvent
from app.models.interview_question import InterviewQuestion
from app.models.plan_common import Counts, DoneCounts, ItemResult, PlanItem
from app.models.weekly_plan import WeeklyPlan
from app.routers import daily_plans, dashboard
from app.routers.daily_plans import get_day
from app.routers.dashboard import dashboard_month_interviews, dashboard_net_monthly, dashboard_week
from app.routers.interviews import follow_ups_summary
from app.stats import profile_loader

pytestmark = [
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]

UTC = timezone.utc
MON, TUE, WED = date(2026, 11, 16), date(2026, 11, 17), date(2026, 11, 18)
TODAY = WED.isoformat()


def at(day: date, hh=12, mm=0) -> datetime:
    """A wall-clock time of `day` in the app timezone."""
    return datetime.combine(day, time(hh, mm), tzinfo=app_tz())


def utc(dt: datetime) -> datetime:
    return dt.astimezone(UTC).replace(tzinfo=None)


def iso(dt: datetime) -> str:
    return utc(dt).isoformat() + "Z"


@pytest.fixture
async def db(monkeypatch):
    profile_loader.clear_cache()
    await init_beanie(database=AsyncMongoMockClient()["home-redesign-unit"], document_models=[
        UserExtra, Account, Bidder, Bid, DailyPlan, WeeklyPlan, Interview, InterviewQuestion, Transaction,
        Application, Caller])
    monkeypatch.setattr(plan_bidder, "RELEASE_DAY", date(2026, 12, 1))
    monkeypatch.setattr(dashboard, "_now", lambda: utc(at(WED, 15)))
    yield
    profile_loader.clear_cache()


@pytest.fixture
async def me(db):
    return await UserExtra(name="Ana", email="ana@x.io").insert()


async def user(name, role="staff") -> UserExtra:
    return await UserExtra(name=name, email=f"{name.lower()}@x.io", role=role).insert()


def rnd(stage="intro", status="scheduled", start=None, **extra) -> dict:
    start = start or at(TUE)
    return InterviewStageEvent(
        stage=stage, at=start, scheduledAt=start, endsAt=start + timedelta(hours=1), status=status, **extra,
    ).model_dump()


async def interview(owner, company, *rounds, account=None) -> Interview:
    return await Interview(
        accountId=account.id if account else PydanticObjectId(), createdBy=owner.id,
        scheduledAt=rounds[-1]["scheduledAt"], companyName=company,
        stageHistory=list(rounds), stage=rounds[-1]["stage"], status=rounds[-1]["status"],
    ).insert()


def moved(frm: datetime, to: datetime) -> list[dict]:
    return [{"at": frm - timedelta(hours=3), "fromAt": frm, "toAt": to, "reason": "company"}]


async def today_rows(who, monkeypatch, now: datetime, day=TUE) -> list[dict]:
    monkeypatch.setattr(daily_plans, "_now", lambda: utc(now))
    return (await get_day(day.isoformat(), who, None))["todayInterviews"]


# --- the day view: todayInterviews / interviewsMarked ---------------------------------------------------------


@pytest.mark.parametrize("hh,mm,state", [
    (11, 59, "upcoming"), (12, 0, "now"), (12, 59, "now"), (13, 0, "unmarked"), (13, 1, "unmarked"),
])
async def test_a_scheduled_round_is_upcoming_now_then_unmarked(me, monkeypatch, hh, mm, state):
    acct = await Account(name="Alpha", region="EU", createdBy=me.id, template="x").insert()
    iv = await interview(me, "Acme", rnd(), account=acct)
    [row] = await today_rows(me, monkeypatch, at(TUE, hh, mm))
    assert row == {
        "interviewId": str(iv.id), "roundId": iv.stageHistory[0].id, "companyName": "Acme", "profileName": "Alpha",
        "stage": "intro", "scheduledAt": iso(at(TUE, 12)), "endsAt": iso(at(TUE, 13)), "status": "scheduled",
        "state": state, "rescheduledTo": None,
        "who": {"kind": "self", "name": None, "assigned": False, "confirmed": False},
        "steps": [{"stage": "intro", "state": "today"}],
    }


@pytest.mark.parametrize("status", ["completed", "passed", "rejected", "canceled"])
async def test_a_marked_round_has_its_status_as_state_whatever_the_time(me, monkeypatch, status):
    await interview(me, "Acme", rnd(status=status))
    for now in (at(TUE, 8), at(TUE, 12, 30), at(TUE, 20)):
        [row] = await today_rows(me, monkeypatch, now)
        assert (row["status"], row["state"]) == (status, status)


async def test_a_round_moved_away_is_rescheduled_on_the_day_it_left_and_normal_on_its_new_day(me, monkeypatch):
    await interview(me, "Acme", rnd(start=at(WED, 10), reschedules=moved(at(TUE, 12), at(WED, 10))))
    [left] = await today_rows(me, monkeypatch, at(TUE, 9))
    assert (left["state"], left["status"], left["rescheduledTo"]) == ("rescheduled", "scheduled", iso(at(WED, 10)))
    assert (left["scheduledAt"], left["endsAt"]) == (iso(at(TUE, 12)), iso(at(TUE, 13)))  # the slot it had that day
    [new] = await today_rows(me, monkeypatch, at(TUE, 9), day=WED)
    assert (new["state"], new["rescheduledTo"], new["scheduledAt"]) == ("upcoming", None, iso(at(WED, 10)))
    assert await today_rows(me, monkeypatch, at(TUE, 9), day=MON) == []


async def test_a_round_moved_within_the_day_is_not_rescheduled(me, monkeypatch):
    await interview(me, "Acme", rnd(start=at(TUE, 16), reschedules=moved(at(TUE, 12), at(TUE, 16))))
    [row] = await today_rows(me, monkeypatch, at(TUE, 9))
    assert (row["state"], row["rescheduledTo"], row["scheduledAt"]) == ("upcoming", None, iso(at(TUE, 16)))


async def test_who_takes_the_round(me, monkeypatch):
    def caller(**kw):
        return {"caller": {"enabled": True, **kw}}

    await interview(me, "Self", rnd(start=at(TUE, 9)))
    await interview(me, "Off", rnd(start=at(TUE, 10), caller={"enabled": False, "callerName": "Cal"}))
    await interview(me, "Confirmed", rnd(start=at(TUE, 11), confirmed=True, **caller(callerName="Cal", callerId="c1")))
    await interview(me, "Named", rnd(start=at(TUE, 12), **caller(callerName="Cal")))
    await interview(me, "Tbd", rnd(start=at(TUE, 13), **caller(callerName="TBD")))
    rows = await today_rows(me, monkeypatch, at(TUE, 8))
    assert [(r["companyName"], r["who"]) for r in rows] == [
        ("Self", {"kind": "self", "name": None, "assigned": False, "confirmed": False}),
        ("Off", {"kind": "self", "name": None, "assigned": False, "confirmed": False}),
        ("Confirmed", {"kind": "caller", "name": "Cal", "assigned": True, "confirmed": True}),
        ("Named", {"kind": "caller", "name": "Cal", "assigned": True, "confirmed": False}),
        ("Tbd", {"kind": "caller", "name": None, "assigned": False, "confirmed": False}),
    ]


async def test_steps_of_a_third_round(me, monkeypatch):
    await interview(
        me, "Acme", rnd(status="passed", start=at(MON - timedelta(days=7))),
        rnd("tech_round_1", "canceled", start=at(MON)), rnd("tech_round_2"),
    )
    [row] = await today_rows(me, monkeypatch, at(TUE, 9))
    assert row["stage"] == "tech_round_2"
    assert row["steps"] == [{"stage": "intro", "state": "passed"}, {"stage": "tech_round_1", "state": "canceled"},
                            {"stage": "tech_round_2", "state": "today"}]


async def test_steps_rejected_held_and_passed_by_a_later_round_without_ai_and_home_rounds(me, monkeypatch):
    early = at(MON - timedelta(days=7))
    await interview(
        me, "Acme", rnd("ai_interview", "passed", start=early), rnd("intro", "completed", start=early),
        rnd("home_assessment", "completed", start=early), rnd("tech_round_1", "rejected", start=early),
        rnd("live_coding", "scheduled", start=at(MON)), rnd("final"),
    )
    [row] = await today_rows(me, monkeypatch, at(TUE, 9))
    assert row["steps"] == [{"stage": "intro", "state": "passed"}, {"stage": "tech_round_1", "state": "rejected"},
                            {"stage": "live_coding", "state": "held"}, {"stage": "final", "state": "today"}]


@pytest.mark.parametrize("stage", ["ai_interview", "home_assessment"])
async def test_ai_interviews_and_home_assessments_are_left_out(me, monkeypatch, stage):
    await interview(me, "Robot", rnd(stage))
    out = await get_day(TUE.isoformat(), me, None)
    assert out["todayInterviews"] == [] and out["interviewsMarked"] == {"marked": 0, "total": 0}


async def test_interviews_marked_and_time_order(me, monkeypatch):
    await interview(me, "Late", rnd(start=at(TUE, 16)))
    await interview(me, "Passed", rnd(status="passed", start=at(TUE, 9)))
    await interview(me, "Moved", rnd(start=at(WED, 10), reschedules=moved(at(TUE, 11), at(WED, 10))))
    await interview(me, "Unmarked", rnd(start=at(TUE, 10)))
    monkeypatch.setattr(daily_plans, "_now", lambda: utc(at(TUE, 12)))
    out = await get_day(TUE.isoformat(), me, None)
    assert [(r["companyName"], r["state"]) for r in out["todayInterviews"]] == [
        ("Passed", "passed"), ("Unmarked", "unmarked"), ("Moved", "rescheduled"), ("Late", "upcoming")]
    assert out["interviewsMarked"] == {"marked": 2, "total": 4}


async def test_another_persons_rounds_never_appear(me, monkeypatch):
    ben, boss = await user("Ben"), await user("Boss", "admin")
    await interview(ben, "Bens", rnd())
    await interview(ben, "Bens moved", rnd(start=at(WED, 10), reschedules=moved(at(TUE, 12), at(WED, 10))))
    # a teammate may read the plan (as before), never the rounds
    for out in (await get_day(TUE.isoformat(), me, None), await get_day(TUE.isoformat(), me, str(ben.id))):
        assert out["todayInterviews"] == [] and out["interviewsMarked"] == {"marked": 0, "total": 0}
    for reader, who in ((ben, None), (ben, str(ben.id)), (boss, str(ben.id))):
        out = await get_day(TUE.isoformat(), reader, who)
        assert len(out["todayInterviews"]) == 2 and out["interviewsMarked"] == {"marked": 1, "total": 2}


# --- GET /dashboard/week --------------------------------------------------------------------------------------


async def week_plan(owner, **kw):
    await WeeklyPlan(
        userId=owner.id, weekStart=datetime(2026, 11, 16), weekNumber=47, year=2026,
        startDate=datetime(2026, 11, 16), endDate=datetime(2026, 11, 22), **kw,
    ).insert()


async def day_plan(owner, d, **kw):
    await DailyPlan(userId=owner.id, date=datetime(2026, 11, d), year=2026, weekNumber=47, **kw).insert()


async def test_bids_by_day_with_a_target(me):
    await week_plan(me, goal=Counts(bidsSelf=500, bidsBidder=500))
    await day_plan(me, 16, done=DoneCounts(bidsSelf=100, bidsBidder=20))
    await day_plan(me, 17, done=DoneCounts(bidsBidder=80), targetOverride=Counts(bidsSelf=300))
    await day_plan(me, 21, done=DoneCounts(bidsSelf=7))  # Saturday: in the total, not a working day
    out = await dashboard_week(me, None, TODAY)
    assert out["bids"]["byDay"] == [
        {"date": "2026-11-16", "done": 120, "self": 100, "bidder": 20, "target": 200}, {"date": "2026-11-17", "done": 80, "self": 0, "bidder": 80, "target": 300},
        {"date": "2026-11-18", "done": 0, "self": 0, "bidder": 0, "target": 200}, {"date": "2026-11-19", "done": 0, "self": 0, "bidder": 0, "target": 200},
        {"date": "2026-11-20", "done": 0, "self": 0, "bidder": 0, "target": 200},
    ]
    assert out["bids"]["total"] == 207 and out["bids"]["pace"]["expected"] == 700


async def test_bids_by_day_without_a_target(me):
    await day_plan(me, 16, done=DoneCounts(bidsSelf=100))
    out = await dashboard_week(me, None, TODAY)
    assert out["bids"]["target"] is None
    assert out["bids"]["byDay"][:2] == [{"date": "2026-11-16", "done": 100, "self": 100, "bidder": 0, "target": None},
                                        {"date": "2026-11-17", "done": 0, "self": 0, "bidder": 0, "target": None}]
    assert [d["date"] for d in out["bids"]["byDay"]] == [f"2026-11-{n}" for n in range(16, 21)]


async def test_week_interview_outcomes(me):
    ben = await user("Ben")
    await interview(me, "Passed", rnd(status="passed", start=at(MON)))
    await interview(me, "Went on", rnd(status="completed", start=at(MON)), rnd("tech_round_1", start=at(WED, 20)))
    await interview(me, "Rejected", rnd(status="rejected", start=at(MON), statusReason="pay"))
    await interview(me, "Silent", rnd(status="rejected", start=at(MON), statusReason="no_answer"))
    await interview(me, "Waiting", rnd(status="completed", start=at(TUE)))
    await interview(me, "Canceled", rnd(status="canceled", start=at(TUE)))
    await interview(me, "Last week", rnd(status="passed", start=at(MON - timedelta(days=3))))
    await interview(me, "Next week", rnd(start=at(MON + timedelta(days=7))))
    await interview(ben, "Bens", rnd(status="passed", start=at(MON)))
    out = await dashboard_week(me, None, TODAY)
    assert out["interviews"]["outcomes"] == {
        "held": 5, "passed": 2, "rejected": 1, "noAnswer": 1, "waiting": 1, "canceled": 1, "scheduled": 1}
    assert (await dashboard_week(ben, None, TODAY))["interviews"]["outcomes"] == {
        "held": 1, "passed": 1, "rejected": 0, "noAnswer": 0, "waiting": 0, "canceled": 0, "scheduled": 0}


def cells(row):
    return [(d["state"], d["value"], d["target"]) for d in row["days"]]


async def test_routines_grid_states_and_sums_equal_the_tally(me):
    await week_plan(me, routines=[
        PlanItem(id="r1", text="Review"), PlanItem(id="r2", text="Contacts", kind="count", target=50),
        PlanItem(id="r3", text="Old", archived=True), PlanItem(id="r4", text="Gone", archived=True),
    ])
    await day_plan(me, 16, results={"r1": ItemResult(done=True), "r2": ItemResult(value=50),
                                    "r3": ItemResult(done=True)})
    await day_plan(me, 17, results={"r2": ItemResult(value=49)})
    await day_plan(me, 18, results={"r2": ItemResult(value=10, target=40)})
    out = (await dashboard_week(me, None, TODAY))["routines"]
    r1, r2, r3 = out["rows"]  # r4 was never shown this week
    assert [d["date"] for d in r1["days"]] == [f"2026-11-{n}" for n in range(16, 21)]
    assert (r1["id"], r1["text"], r1["doneDays"], r1["totalDays"]) == ("r1", "Review", 1, 3)
    assert cells(r1) == [("done", None, None), ("missed", None, None), ("today", None, None),
                         ("future", None, None), ("future", None, None)]
    assert cells(r2) == [("done", None, None), ("short", 49, 50), ("short", 10, 40),
                         ("future", None, None), ("future", None, None)]
    assert (r2["doneDays"], r2["totalDays"]) == (1, 3)
    assert cells(r3) == [("done", None, None)] + [("off", None, None)] * 4
    assert (r3["doneDays"], r3["totalDays"]) == (1, 1)
    assert (out["done"], out["expected"]) == (3, 7)
    assert sum(r["doneDays"] for r in out["rows"]) == out["done"]
    assert sum(r["totalDays"] for r in out["rows"]) == out["expected"]


async def test_routines_grid_on_the_last_day_and_with_no_plan(me):
    assert (await dashboard_week(me, None, TODAY))["routines"] == {"done": 0, "expected": 0, "rows": []}
    await week_plan(me, routines=[PlanItem(id="r2", text="Contacts", kind="count", target=50)])
    await day_plan(me, 20, results={"r2": ItemResult(value=60)})
    out = (await dashboard_week(me, None, "2026-11-21"))["routines"]  # Saturday: every working day is past
    assert [d["state"] for d in out["rows"][0]["days"]] == ["missed"] * 4 + ["done"]
    assert (out["rows"][0]["doneDays"], out["rows"][0]["totalDays"]) == (out["done"], out["expected"]) == (1, 5)


# --- GET /dashboard/net-monthly -------------------------------------------------------------------------------


async def test_net_monthly_in_and_out(me):
    for amount, status in ((1000, "approved"), (250.5, "approved"), (-300, "approved"), (-99, "pending")):
        await Transaction(userId=me.id, date=datetime(2026, 11, 5), amount=amount, status=status).insert()
    out = await dashboard_net_monthly(me, userId=None, today=TODAY)
    nov = next(m for m in out["months"] if m["period"] == "2026-11")
    assert nov == {"period": "2026-11", "income": 1250.5, "outcome": 300.0, "net": 950.5, "in": 1250.5, "out": 300.0}
    assert out["months"][0]["in"] == out["months"][0]["out"] == 0.0


# --- GET /dashboard/month-interviews --------------------------------------------------------------------------


async def many(owner, n, status="passed", day=date(2026, 11, 5)):
    for _ in range(n):
        await interview(owner, "Co", rnd(status=status, start=at(day)))


async def month(who, user_id=None):
    profile_loader.clear_cache()
    return await dashboard_month_interviews(who, user_id, TODAY)


async def test_month_interviews_with_nothing(me):
    assert await month(me) == {
        "pipeline": [{"stage": "intro", "label": "Intro", "count": 0}, {"stage": "tech", "label": "Tech", "count": 0},
                     {"stage": "hiring", "label": "Hiring", "count": 0},
                     {"stage": "panel", "label": "Panel", "count": 0}, {"stage": "final", "label": "Final", "count": 0}],
        "waitingForResult": 0, "followUpsDue": 0,
        "passRate": {"value": None, "passed": 0, "decided": 0, "few": True, "teamRate": None},
    }


async def test_month_pass_rate_is_few_under_twenty_decided_and_carries_the_team_rate(me):
    ben = await user("Ben")
    await many(me, 19)
    await many(me, 3, day=date(2026, 10, 28))  # last month
    await many(me, 2, status="scheduled", day=date(2026, 11, 27))  # not decided
    await many(ben, 1, status="rejected")
    assert (await month(me))["passRate"] == {"value": 100.0, "passed": 19, "decided": 19, "few": True,
                                              "teamRate": 95.0}
    await many(me, 1, status="rejected")
    assert (await month(me))["passRate"] == {"value": 95.0, "passed": 19, "decided": 20, "few": False,
                                              "teamRate": 90.5}
    assert (await month(ben))["passRate"] == {"value": 0.0, "passed": 0, "decided": 1, "few": True, "teamRate": 90.5}


async def test_month_pass_rate_is_null_with_nothing_decided(me):
    await interview(me, "Waiting", rnd(status="completed", start=at(TUE)))
    await interview(me, "Canceled", rnd(status="canceled", start=at(TUE)))
    assert (await month(me))["passRate"] == {"value": None, "passed": 0, "decided": 0, "few": True, "teamRate": None}


async def test_month_pipeline_waiting_for_a_result_and_follow_ups_due(me):
    ben = await user("Ben")
    await interview(me, "This week", rnd(status="passed", start=at(MON)), rnd("tech_round_1", start=at(WED, 20)))
    await interview(me, "Waiting", rnd(status="completed", start=at(TUE)))
    await interview(me, "Silent for weeks", rnd("final", "completed", start=at(date(2026, 10, 20))))  # needs a result
    await interview(me, "Went on", rnd(status="completed", start=at(date(2026, 11, 2))),
                    rnd("cultural", "rejected", start=at(date(2026, 11, 9)), statusReason="pay"))
    await interview(me, "Before the data start", rnd(status="completed", start=at(date(2026, 10, 1))))
    await interview(me, "Robot", rnd("ai_interview", "completed", start=at(TUE)))
    await interview(ben, "Bens", rnd(status="completed", start=at(TUE)))
    out = await month(me)
    assert [(s["stage"], s["count"]) for s in out["pipeline"]] == [
        ("intro", 2), ("tech", 1), ("hiring", 0), ("panel", 0), ("final", 0)]
    week = await dashboard_week(me, None, TODAY)
    assert [(s["key"], s["label"], s["count"]) for s in week["stages"][:-1]] == [
        (s["stage"], s["label"], s["count"]) for s in out["pipeline"]]
    assert out["waitingForResult"] == 2
    assert out["followUpsDue"] == (await follow_ups_summary(me.id, WED))["toDo"] >= 1  # "Silent for weeks"
    assert (await month(ben))["waitingForResult"] == 1


async def test_month_interviews_are_your_own_unless_an_admin_asks(me):
    ben, boss = await user("Ben"), await user("Boss", "admin")
    await many(ben, 2)
    for who in (str(ben.id), str(me.id)):
        with pytest.raises(HTTPException) as e:
            await month(me, who)
        assert e.value.status_code == 403
    assert (await month(me))["passRate"]["decided"] == 0
    assert (await month(boss, str(ben.id)))["passRate"]["decided"] == 2
    assert (await month(boss))["passRate"]["decided"] == 0
