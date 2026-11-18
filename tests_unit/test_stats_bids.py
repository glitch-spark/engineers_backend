"""Statistics → Bids view and rules (mongomock)."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")
os.environ["RESUME_QUEUE_DISABLED"] = "1"

from datetime import date, datetime

import pytest
from beanie import init_beanie
from mongomock_motor import AsyncMongoMockClient

from app.models import Account, DailyPlan, Interview, InterviewQuestion, UserExtra
from app.models.bid import Bid
from app.models.bidder import Bidder
from app.models.plan_common import Counts
from app.models.weekly_plan import WeeklyPlan
from app.stats import bids as sb

pytestmark = [
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]

TODAY = date(2026, 11, 18)  # Wednesday; 13 working days elapsed in November


# ---- pure rules ----

def test_low_self_share_fires_below_team_minus_15pts():
    tip = sb.low_self_share(214, 519, [0.58, 0.6, 0.55])
    assert tip["severity"] == "bad" and tip["id"] == "bids.low_self_share"
    assert "41%" in tip["text"] and "58%" in tip["text"] and tip["impact"] > 0


def test_low_self_share_quiet_when_close_or_no_total():
    assert sb.low_self_share(280, 519, [0.58, 0.6, 0.55]) is None  # 54%: within 15 pts
    assert sb.low_self_share(0, 0, [0.58]) is None
    assert sb.low_self_share(50, 100, [0.5, 0.5, 0.5]) is None  # everyone the same


def test_weak_weekdays_fires_with_two_samples():
    per = {0: [50, 50], 1: [20, 22], 2: [50, 48], 3: [21, 23], 4: [50, 52]}
    tip = sb.weak_weekdays(per)
    assert tip["severity"] == "watch" and "Tuesdays" in tip["text"] and "Thursdays" in tip["text"]
    assert "Mondays" not in tip["text"]


def test_weak_weekdays_needs_two_samples():
    per = {0: [50], 1: [5], 2: [50], 3: [50], 4: [50]}
    assert sb.weak_weekdays(per) is None


def test_idle_profiles_names_idle_ones():
    tip = sb.idle_profiles({"Ana-EU": None, "Ana-US": date(2026, 11, 10), "Ana-LA": date(2026, 11, 17)}, TODAY)
    assert tip["severity"] == "watch" and "2 profiles" in tip["text"] and "Ana-EU" in tip["text"]
    assert "Ana-LA" not in tip["text"]
    assert sb.idle_profiles({"Ana-LA": date(2026, 11, 17)}, TODAY) is None


def test_behind_target_fires_and_skipped_when_no_target():
    tip = sb.behind_target(400, 600)
    assert tip["severity"] == "bad" and tip["impact"] == 200
    assert sb.behind_target(400, 0) is None
    assert sb.behind_target(520, 600) is None  # 87%


def test_up_vs_last_period_good():
    tip = sb.up_vs_last_period(590, 500)
    assert tip["severity"] == "good" and "+18%" in tip["text"]
    assert sb.up_vs_last_period(500, 0) is None
    assert sb.up_vs_last_period(520, 500) is None


# ---- view ----

@pytest.fixture
async def db():
    await init_beanie(
        database=AsyncMongoMockClient()["stats-bids"],
        document_models=[UserExtra, Account, DailyPlan, WeeklyPlan, Interview, InterviewQuestion, Bid, Bidder],
    )


async def _user(name, role="staff"):
    return await UserExtra(name=name, email=f"{name.lower()}@x.io", role=role).insert()


async def _plan(user, d, logged=False, **done):
    await DailyPlan(userId=user.id, date=datetime(2026, 11, d), year=2026, weekNumber=47, done=done,
                    loggedAt=datetime(2026, 11, d, 20) if logged else None).insert()


async def _team(per_day=(40, 50, 60, 30)):
    """Ana + 3 staff, each typing `per_day` self applies on Nov 2-6 and 9-13 (10 days)."""
    users = [await _user(n) for n in ("Ana", "Ben", "Cy", "Dee")]
    for u, n in zip(users, per_day):
        for d in (2, 3, 4, 5, 6, 9, 10, 11, 12, 13):
            await _plan(u, d, bidsSelf=n)
    return users


KEYS = {"tab", "label", "subject", "headline", "tips", "everyone", "trend", "notEnoughData", "definitions"}


async def test_bids_view_contract_keys(db):
    ana, *_ = await _team()
    out = await sb.bids_view(ana, "month", TODAY, "all", None, TODAY)
    assert set(out) == KEYS and out["tab"] == "bids"
    assert out["subject"] == {"userId": str(ana.id), "name": "Ana"}
    assert [h["key"] for h in out["headline"]] == ["bids", "rank", "perDay"]
    h = out["headline"][0]
    assert h["value"] == 400 and h["rank"] == 3 and h["of"] == 4 and h["team"]["n"] == 4
    assert h["note"] == "Self 400 · bidder 0"
    per_day = out["headline"][2]
    assert per_day["value"] == 31 and "team" in per_day["note"] and "target" not in per_day["note"]  # 400 / 13
    assert set(out["everyone"]) == {"metric", "rows"} and out["trend"]["unit"] == "week"
    assert out["notEnoughData"] is False and len(out["definitions"]) == 2


async def test_unposted_day_self_bids_count(db):
    ana, *_ = await _team()
    await _plan(ana, 16, logged=False, bidsSelf=7)
    out = await sb.bids_view(ana, "month", TODAY, "all", None, TODAY)
    assert out["headline"][0]["value"] == 407


async def test_subject_outside_team_would_rank(db):
    await _team()
    admin = await _user("Root", "admin")
    await _plan(admin, 2, bidsSelf=45)
    out = await sb.bids_view(admin, "month", TODAY, "all", None, TODAY)
    h = out["headline"][0]
    assert h["team"]["n"] == 4 and h["of"] == 5 and h["rank"] == 5  # 45 is below every team member; not in the median
    assert [r["isSubject"] for r in out["everyone"]["rows"]].count(True) == 1


async def test_not_enough_data_early_in_month(db):
    ana, *_ = await _team()
    out = await sb.bids_view(ana, "month", date(2026, 11, 2), "all", None, date(2026, 11, 2))
    assert out["notEnoughData"] is True and out["tips"] == [] and out["headline"]


async def test_region_filter_excludes_other_regions(db):
    ana, ben, cy, dee = await _team()
    await Account(name="Ana US", createdBy=ana.id, region="US").insert()
    for u in (ana, ben):
        await _plan(u, 16, bidsSelfUs=9)
    out = await sb.bids_view(ana, "month", TODAY, "US", None, TODAY)
    assert out["headline"][0]["value"] == 9 and out["headline"][0]["team"]["n"] == 2
    assert out["notEnoughData"] is True  # two people


async def test_everyone_sorted_with_subject_flag(db):
    ana, *_ = await _team()
    rows = (await sb.bids_view(ana, "month", TODAY, "all", None, TODAY))["everyone"]["rows"]
    assert [r["name"] for r in rows] == ["Cy", "Ben", "Ana", "Dee"]
    assert [r["rank"] for r in rows] == [1, 2, 3, 4]
    assert [r["isSubject"] for r in rows] == [False, False, True, False]
    assert rows[2]["parts"] == {"self": 400, "bidder": 0}


async def test_behind_target_from_weekly_goal(db):
    ana, *_ = await _team()
    await WeeklyPlan(userId=ana.id, weekStart=datetime(2026, 11, 2), weekNumber=45, year=2026,
                     startDate=datetime(2026, 11, 2), endDate=datetime(2026, 11, 8), goal=Counts(bidsSelf=1000)).insert()
    out = await sb.bids_view(ana, "month", TODAY, "all", None, TODAY)
    tip = next(t for t in out["tips"] if t["id"] == "bids.behind_target")
    assert tip["evidence"]["target"] == 1000 and tip["evidence"]["actual"] == 400 and "impact" not in tip
    assert "target" in out["headline"][2]["note"]


async def test_idle_profiles_ignores_archived(db):
    ana, *_ = await _team()
    await Account(name="Ana-EU", createdBy=ana.id, region="EU").insert()
    await Account(name="Old-US", createdBy=ana.id, region="US", archived=True).insert()
    out = await sb.bids_view(ana, "month", TODAY, "all", None, TODAY)
    tip = next(t for t in out["tips"] if t["id"] == "bids.idle_profiles")
    assert "Ana-EU" in tip["text"] and "Old-US" not in tip["text"]
