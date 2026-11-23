"""Interview follow-ups in the daily standup: `followUps` in the day view, and the Follow-ups line in the Slack form
and the posted update (mongomock-motor; Slack faked).
docs/superpowers/specs/2026-11-20-followups-home-standup-design.md"""
import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

import json
from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace

import pytest
from beanie import PydanticObjectId, init_beanie
from mongomock_motor import AsyncMongoMockClient

from app import plan_bidder
from app.dashboard_stats import app_tz
from app.models import Account, UserExtra
from app.models.bid import Bid
from app.models.bidder import Bidder
from app.models.daily_plan import DailyPlan
from app.models.interview import Interview, InterviewStageEvent
from app.models.interview_question import InterviewQuestion
from app.models.plan_common import Counts
from app.models.weekly_plan import WeeklyPlan
from app.routers import daily_plans
from app.routers.daily_plans import _sync_slack as REAL_SYNC_SLACK, get_day, put_day
from app.routers.weekly_plans import put_week
from app.slack_bot import daily_plan_text, follow_ups_line
from app.standup.modal import build_view

pytestmark = [
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]

UTC = timezone.utc
WEEK = "2026-11-09"
MON, TUE, WED = date(2026, 11, 9), date(2026, 11, 10), date(2026, 11, 11)
HELD = datetime(2026, 10, 20, 15, 0, tzinfo=UTC)  # after the data start, long past
NONE = {"doneToday": [], "toDo": 0}


@pytest.fixture
async def db(monkeypatch):
    await init_beanie(database=AsyncMongoMockClient()["standup-follow-ups-unit"], document_models=[
        UserExtra, Account, Bidder, Bid, DailyPlan, WeeklyPlan, Interview, InterviewQuestion])
    monkeypatch.setattr(daily_plans, "_today", lambda: date(2026, 11, 14))
    monkeypatch.setattr(plan_bidder, "RELEASE_DAY", date(2026, 12, 1))

    async def fake_sync(plan, owner, done, rounds):
        return None

    monkeypatch.setattr(daily_plans, "_sync_slack", fake_sync)


@pytest.fixture
async def me(db):
    return await UserExtra(name="Ana", email="ana@x.io", plansOwnWeek=True, slackUserId="U0ANA0001").insert()


def at(day: date, hh=12, mm=0) -> datetime:
    """A wall-clock time of `day` in the app timezone."""
    return datetime.combine(day, time(hh, mm), tzinfo=app_tz())


def ago(**kw) -> datetime:
    return datetime.now(UTC).replace(microsecond=0) - timedelta(**kw)


def rnd(status="completed", start=HELD, follow_ups=(), stage="intro", **extra) -> dict:
    return InterviewStageEvent(
        stage=stage, at=start, scheduledAt=start, endsAt=start + timedelta(hours=1), status=status,
        followUps=[{"at": t} for t in follow_ups], **extra,
    ).model_dump()


async def interview(owner, company, *rounds) -> Interview:
    return await Interview(
        accountId=PydanticObjectId(), createdBy=owner.id, scheduledAt=rounds[-1]["scheduledAt"], companyName=company,
        stageHistory=list(rounds), stage=rounds[-1]["stage"], status=rounds[-1]["status"],
    ).insert()


async def follow_ups(me, day=TUE) -> dict:
    return (await get_day(day.isoformat(), me, None))["followUps"]


def context_lines(view) -> list[str]:
    return [e["text"] for b in view["blocks"] if b["type"] == "context" for e in b["elements"]]


# --- the day view --------------------------------------------------------------------------------------------


async def test_day_view_always_carries_follow_ups(me):
    assert await follow_ups(me) == NONE
    out = await put_day(TUE.isoformat(), me, {"notes": "x"})
    assert out["followUps"] == NONE
    assert not hasattr(await DailyPlan.find_one({"userId": me.id}), "followUps")  # not stored on the plan


async def test_a_follow_up_made_that_day_is_listed(me):
    await interview(me, "Today", rnd(follow_ups=[at(TUE)]))
    await interview(me, "Yesterday", rnd(follow_ups=[at(MON)]))
    await interview(me, "Tomorrow", rnd(follow_ups=[at(WED, 0, 30)]))
    await interview(me, "Late", rnd(follow_ups=[at(TUE, 23, 30)]))
    await interview(me, "Never", rnd())
    assert (await follow_ups(me))["doneToday"] == ["Today", "Late"]
    assert (await follow_ups(me, MON))["doneToday"] == ["Yesterday"]


async def test_done_today_is_in_the_order_done_one_entry_per_round(me):
    await interview(me, "Third", rnd(follow_ups=[at(TUE, 16)]))
    await interview(me, "First", rnd(follow_ups=[at(MON), at(TUE, 9), at(TUE, 17)]))  # twice that day: once
    await interview(me, None, rnd(follow_ups=[at(TUE, 10)]))  # no company name
    await interview(me, "Two rounds", rnd(status="passed", follow_ups=[at(TUE, 11)]),
                    rnd(stage="final", start=HELD + timedelta(days=5), follow_ups=[at(TUE, 12)]))
    assert (await follow_ups(me))["doneToday"] == ["First", "—", "Two rounds", "Two rounds", "Third"]


async def test_done_today_is_the_whole_list(me):
    for n in range(7):
        await interview(me, f"C{n}", rnd(follow_ups=[at(TUE, 9 + n)]))
    assert (await follow_ups(me))["doneToday"] == [f"C{n}" for n in range(7)]


async def test_to_do_counts_follow_up_and_heard_back_rounds_as_of_now(me):
    await interview(me, "Follow up", rnd(start=ago(days=8)))
    await interview(me, "Heard back", rnd(start=ago(days=20), follow_ups=[ago(days=8)]))
    await interview(me, "Between", rnd(start=ago(days=3)))  # held under 7 days ago
    await interview(me, "Waiting", rnd(start=ago(days=20), follow_ups=[ago(days=2)]))
    await interview(me, "Outcome", rnd(status="scheduled", start=ago(days=1)))
    await interview(me, "Reason", rnd(status="canceled", start=ago(days=1)))
    await interview(me, "Not latest", rnd(start=ago(days=9)), rnd(status="scheduled", stage="final", start=ago(days=-9)))
    await interview(me, "Old", rnd(start=datetime(2026, 10, 5, 15, 0, tzinfo=UTC)))  # before the data start
    assert await follow_ups(me) == {"doneToday": [], "toDo": 2}
    assert (await follow_ups(me, MON))["toDo"] == 2  # whichever day is shown


async def test_only_the_persons_own_rounds_also_for_admins(me):
    admin = await UserExtra(name="Root", email="root@x.io", role="admin").insert()
    await interview(me, "Ana's", rnd(start=ago(days=8)))
    await interview(me, "Ana's done", rnd("passed", follow_ups=[at(TUE)]))
    assert await follow_ups(admin) == NONE
    assert await follow_ups(me) == {"doneToday": ["Ana's done"], "toDo": 1}
    # An admin reading Ana's day reads Ana's.
    assert (await get_day(TUE.isoformat(), admin, str(me.id)))["followUps"] == {"doneToday": ["Ana's done"], "toDo": 1}


# --- the line ------------------------------------------------------------------------------------------------


def test_the_three_forms_and_the_left_out_case():
    both = {"doneToday": ["Acme", "Globex"], "toDo": 3}
    assert follow_ups_line(both) == "Follow-ups · done today: Acme, Globex · still to do: 3"
    assert follow_ups_line(both, label="*Follow-ups*") == "*Follow-ups* · done today: Acme, Globex · still to do: 3"
    assert follow_ups_line({"doneToday": [], "toDo": 3}) == "Follow-ups · still to do: 3"
    assert follow_ups_line({"doneToday": ["Acme", "Globex"], "toDo": 0}) == "Follow-ups · done today: Acme, Globex"
    assert follow_ups_line(NONE) == "" and follow_ups_line(None) == ""


def test_more_than_five_are_cut_with_a_count():
    names = [f"C{n}" for n in range(7)]
    assert follow_ups_line({"doneToday": names, "toDo": 0}) == "Follow-ups · done today: C0, C1, C2, C3, C4 +2"
    assert follow_ups_line({"doneToday": names[:5], "toDo": 1}) == \
        "Follow-ups · done today: C0, C1, C2, C3, C4 · still to do: 1"


# --- the Slack form ------------------------------------------------------------------------------------------


async def test_form_shows_the_line_after_interviews(me):
    await interview(me, "Acme", rnd("passed", follow_ups=[at(TUE, 9)]))
    await interview(me, "Globex", rnd("passed", follow_ups=[at(TUE, 10)]))
    await interview(me, "Open", rnd(start=ago(days=8)))
    lines = context_lines(await build_view(me, TUE))
    assert lines[1].startswith("*Interviews*")
    assert lines[2] == "*Follow-ups* · done today: Acme, Globex · still to do: 1"
    assert context_lines(await build_view(me, MON))[2] == "*Follow-ups* · still to do: 1"


async def test_form_with_nothing_left_and_with_nothing_at_all(me):
    assert not any("Follow-ups" in ln for ln in context_lines(await build_view(me, TUE)))
    await interview(me, "Acme", rnd("passed", follow_ups=[at(TUE, 9)]))
    assert context_lines(await build_view(me, TUE))[2] == "*Follow-ups* · done today: Acme"
    view = await build_view(me, MON)
    assert not any("Follow-ups" in ln for ln in context_lines(view))
    assert [b["type"] for b in view["blocks"]] == ["context", "context", "input"]


async def test_form_block_budget_holds_with_the_extra_line(me):
    await put_week(WEEK, me, {
        "goal": {"bidsSelf": 100, "bidsBidder": 50},
        "routines": [{"text": f"Routine {n:02}", "kind": "count", "target": 5} for n in range(20)],
        "weeklyGoals": [{"text": f"Goal {n:02}", "kind": "count", "target": 5} for n in range(20)],
        "recapNotes": "",
    })
    for n in range(70):
        await Account(name=f"P{n:02}", createdBy=me.id, region="US").insert()
    await interview(me, "Acme", rnd("passed", follow_ups=[at(TUE, 9)]))
    view = await build_view(me, TUE)
    assert len(view["blocks"]) == 100
    assert context_lines(view)[2] == "*Follow-ups* · done today: Acme"
    assert "open the app" in json.dumps(view["blocks"][-2]) and view["blocks"][-1]["block_id"] == "notes"


# --- the posted update ---------------------------------------------------------------------------------------


def _text(**kw) -> list[str]:
    plan = SimpleNamespace(id="p", date=datetime(2026, 11, 10), done=Counts(), focus=[], notes="Extra note",
                           slackChannel=None, slackTs=None)
    owner = SimpleNamespace(name="Ana", email="a@x.io", slackUserId="U1")
    return daily_plan_text(plan, owner, **kw).splitlines()


def test_posted_update_has_the_line_after_the_interviews_rows():
    lines = _text(follow_ups={"doneToday": ["Acme", "Globex"], "toDo": 3}, week_interviews=(1, 5))
    i = lines.index("2️⃣ Interviews")
    assert lines[i + 3].strip() == "Week              1 / 5"
    assert lines[i + 4] == "   Follow-ups · done today: Acme, Globex · still to do: 3"
    assert lines[i + 5] == "3️⃣ Extra"
    assert _text(follow_ups={"doneToday": [], "toDo": 3})[i + 3] == "   Follow-ups · still to do: 3"
    assert _text(follow_ups={"doneToday": ["Acme", "Globex"], "toDo": 0})[i + 3] == \
        "   Follow-ups · done today: Acme, Globex"
    assert _text(follow_ups={"doneToday": ["a`b <c>"], "toDo": 0})[i + 3] == "   Follow-ups · done today: aˋb &lt;c&gt;"


def test_posted_update_leaves_the_line_out_when_there_is_nothing():
    for kw in ({}, {"follow_ups": NONE}, {"follow_ups": None}):
        assert not any("Follow-ups" in ln for ln in _text(**kw))


async def test_real_sync_slack_hands_the_post_the_days_follow_ups(me, monkeypatch):
    seen = {}

    async def fake_post(plan, owner, **view):
        seen.update(view)
        return "C1", "ts9"

    await interview(me, "Acme", rnd("passed", follow_ups=[at(TUE, 9)]))
    await interview(me, "Open", rnd(start=ago(days=8)))
    plan = await DailyPlan(userId=me.id, date=datetime(2026, 11, 10), year=2026, weekNumber=46,
                           submittedAt=datetime(2026, 11, 10, 20)).insert()
    monkeypatch.setattr(daily_plans, "_sync_slack", REAL_SYNC_SLACK)
    monkeypatch.setattr(daily_plans, "sync_daily_plan_message", fake_post)
    await daily_plans._sync_slack(plan, me, Counts(), {})
    assert seen["follow_ups"] == {"doneToday": ["Acme"], "toDo": 1}
