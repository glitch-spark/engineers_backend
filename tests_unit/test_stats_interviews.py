"""Statistics → Interviews view and rules (mongomock)."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")
os.environ["RESUME_QUEUE_DISABLED"] = "1"

from datetime import date, datetime

import pytest
from beanie import init_beanie
from mongomock_motor import AsyncMongoMockClient

from app import interview_rounds
from app.dashboard_stats import app_tz
from app.models import Account, DailyPlan, Interview, InterviewQuestion, UserExtra
from app.models.bid import Bid
from app.models.bidder import Bidder
from app.models.weekly_plan import WeeklyPlan
from app.stats import interviews as si

pytestmark = [
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]

TODAY = date(2026, 11, 18)


# ---- pure rules ----

def test_low_conversion_needs_30_bids():
    tip = si.low_conversion(0.01, 0.05, 100)
    assert tip["severity"] == "bad" and tip["id"] == "interviews.low_conversion" and tip["impact"] > 0
    assert "1.0" in tip["text"] and "5.0" in tip["text"]
    assert si.low_conversion(0.01, 0.05, 29) is None
    assert si.low_conversion(0.04, 0.05, 100) is None  # 80% of the team: fine
    assert si.low_conversion(None, 0.05, 100) is None and si.low_conversion(0.01, None, 100) is None


def test_stall_needs_four_decided():
    team = {"tech": (9, 11)}
    assert si.stalls_at_stage({"tech": (1, 3)}, team)["severity"] == "watch"
    assert si.stalls_at_stage({"tech": (1, 2)}, team) is None  # 3 decided
    assert si.stalls_at_stage({"tech": (1, 3)}, {"tech": (1, 2)}) is None  # team too thin
    assert si.stalls_at_stage({"tech": (2, 2)}, team) is None  # 50% vs 45%


def test_stall_names_group():
    tip = si.stalls_at_stage({"tech": (1, 4), "final": (1, 3)}, {"tech": (9, 11), "final": (9, 11)})
    assert "Tech rounds" in tip["text"] and "20%" in tip["text"] and "45%" in tip["text"]
    assert tip["evidence"]["group"] == "tech"
    tip = si.stalls_at_stage({"panel": (0, 5)}, {"panel": (9, 11)})
    assert "Panel rounds" in tip["text"]


def test_caller_vs_self_gap():
    tip = si.caller_vs_self((5, 3), (2, 3))  # 62% vs 40%
    assert tip["severity"] == "watch" and "62%" in tip["text"] and "40%" in tip["text"]
    assert si.caller_vs_self((5, 3), (3, 3)) is None  # 62 vs 50: gap < 15
    assert si.caller_vs_self((3, 0), (1, 2)) is None  # 3 decided
    assert si.caller_vs_self((5, 3), (1, 2)) is None
    assert si.caller_vs_self((2, 3), (5, 3)) is None  # callers worse: not this tip


def test_up_vs_last_period_interviews():
    tip = si.up_vs_last_period(12, 10, "Oct 1–18")
    assert tip["severity"] == "good" and "interviews" in tip["text"] and "vs Oct 1–18" in tip["detail"]
    assert si.up_vs_last_period(10, 10) is None


# ---- view ----

@pytest.fixture
async def db():
    await init_beanie(
        database=AsyncMongoMockClient()["stats-interviews"],
        document_models=[UserExtra, Account, DailyPlan, WeeklyPlan, Interview, InterviewQuestion, Bid, Bidder],
    )


async def _user(name, role="staff"):
    return await UserExtra(name=name, email=f"{name.lower()}@x.io", role=role).insert()


async def _bids(user, n, d=2, **extra):
    await DailyPlan(userId=user.id, date=datetime(2026, 11, d), year=2026, weekNumber=45,
                    done={"bidsSelf": n, **extra}).insert()


async def _iv(user, acc, *rounds):
    """rounds: (stage, day, status) or (stage, day, status, True) for a caller round; day = Nov `day`, noon."""
    history = [{"stage": r[0], "at": datetime(2026, 11, r[1], 12), "scheduledAt": datetime(2026, 11, r[1], 12),
                "status": r[2], "caller": {"enabled": True} if len(r) > 3 and r[3] else None} for r in rounds]
    await Interview(accountId=acc.id, createdBy=user.id, scheduledAt=history[-1]["at"], companyName="Acme",
                    stageHistory=history).insert()


async def _team():
    """Ana, Ben, Cy, Dee with 100 bids each and a different number of interviews (4, 2, 2, 1)."""
    users, accs = [], []
    for n in ("Ana", "Ben", "Cy", "Dee"):
        u = await _user(n)
        users.append(u)
        accs.append(await Account(name=f"{n} EU", createdBy=u.id, region="EU").insert())
        await _bids(u, 100)
    ana, ben, cy, dee = users
    a, b, c, d = accs
    for day in (3, 4, 5, 9):
        await _iv(ana, a, ("intro", day, "passed"))
    await _iv(ben, b, ("intro", 3, "passed"), ("tech_round_1", 4, "passed"))
    await _iv(cy, c, ("intro", 5, "passed"))
    await _iv(cy, c, ("intro", 6, "rejected"))
    await _iv(dee, d, ("intro", 6, "rejected"))
    return users, accs


KEYS = {"tab", "label", "subject", "headline", "tips", "everyone", "trend", "notEnoughData", "definitions"}


async def test_contract_keys_and_headline_order(db):
    (ana, *_), _ = await _team()
    out = await si.interviews_view(ana, "month", TODAY, "all", None, TODAY)
    assert set(out) == KEYS and out["tab"] == "interviews"
    assert out["subject"] == {"userId": str(ana.id), "name": "Ana"}
    assert [h["key"] for h in out["headline"]] == ["per100", "rank", "interviews", "reach"]
    per100, rank, ivs, reach = out["headline"]
    assert per100["value"] == 4.0 and per100["format"] == "per100" and per100["note"] == "4 interviews from 100 bids"
    assert per100["team"]["n"] == 4 and per100["rank"] == 1 and per100["of"] == 4 and per100["higherIsBetter"] is True
    assert rank["value"] == 1 and rank["format"] == "rank" and rank["team"] is None and rank["note"] == "of 4"
    assert ivs["value"] == 4 and ivs["format"] == "count" and ivs["note"] == "Self 4 · caller 0"
    assert reach["format"] == "pct" and reach["value"] == 0.0 and reach["note"] == "final 0%"
    assert out["everyone"]["metric"] == "per100" and out["trend"]["unit"] == "week"
    assert len(out["definitions"]) == 3 and out["notEnoughData"] is False


async def test_under_30_bids_listed_not_ranked(db):
    (ana, *_), (acc, *_) = await _team()
    low = await _user("Low")
    lacc = await Account(name="Low EU", createdBy=low.id, region="EU").insert()
    await _bids(low, 10)
    await _iv(low, lacc, ("intro", 3, "passed"), ("tech_round_1", 4, "passed"))
    out = await si.interviews_view(ana, "month", TODAY, "all", None, TODAY)
    rows = out["everyone"]["rows"]
    assert rows[-1]["name"] == "Low" and rows[-1]["rank"] is None and rows[-1]["value"] == 20.0
    assert rows[-1]["parts"] == {"self": 2, "caller": 0}
    assert [r["rank"] for r in rows[:-1]] == [1, 2, 2, 4]
    assert out["headline"][0]["team"]["n"] == 4  # Low is not in the rate distribution
    out = await si.interviews_view(low, "month", TODAY, "all", None, TODAY)
    assert out["headline"][0]["rank"] is None and out["headline"][1]["value"] is None
    assert "interviews.low_conversion" not in [t["id"] for t in out["tips"]]
    assert out["headline"][2]["team"]["n"] == 5  # but is in the interview-count distribution


async def test_zero_bids_rate_is_none_no_crash(db):
    z = await _user("Zed")
    acc = await Account(name="Z", createdBy=z.id, region="EU").insert()
    await _iv(z, acc, ("intro", 3, "passed"))
    out = await si.interviews_view(z, "month", TODAY, "all", None, TODAY)
    assert out["headline"][0]["value"] is None and out["headline"][0]["team"]["n"] == 0
    assert out["everyone"]["rows"][0]["value"] is None and out["notEnoughData"] is True


async def test_region_scopes_interviews_and_bids(db):
    (ana, *_), (a, *_) = await _team()
    us = await Account(name="Ana US", createdBy=ana.id, region="US").insert()
    await _bids(ana, 40, d=10, bidsSelfUs=40)
    await _iv(ana, us, ("intro", 10, "passed"), ("tech_round_1", 11, "passed"))
    out = await si.interviews_view(ana, "month", TODAY, "US", None, TODAY)
    per100, _, ivs, _ = out["headline"]
    assert ivs["value"] == 2 and per100["value"] == 5.0 and per100["note"] == "2 interviews from 40 bids"
    assert out["headline"][2]["team"]["n"] == 1  # only Ana has anything in US


async def test_reach_counts_processes_reaching_tech(db):
    (ana, *_), (a, *_) = await _team()
    await _iv(ana, a, ("intro", 10, "passed"), ("tech_round_1", 11, "passed"), ("final", 12, "passed"))  # tech + final
    await _iv(ana, a, ("intro", 10, "passed"), ("panel", 11, "rejected"))  # tech-or-later via panel
    # Ana now has 4 intro-only + these 2 = 6 processes; 2 reached tech, 1 reached final
    out = await si.interviews_view(ana, "month", TODAY, "all", None, TODAY)
    reach = out["headline"][3]
    assert reach["value"] == pytest.approx(100 * 2 / 6, abs=0.1) and reach["note"] == "final 17%"
    assert out["headline"][2]["value"] == 9  # 4 + 3 + 2 rounds
    assert reach["team"]["n"] == 4


async def test_no_interviews_tip(db):
    await _team()
    zed = await _user("Zed")
    await _bids(zed, 50)
    out = await si.interviews_view(zed, "month", TODAY, "all", None, TODAY)
    tip = next(t for t in out["tips"] if t["id"] == "interviews.no_interviews")
    assert tip["severity"] == "bad" and "No interviews" in tip["text"]
    ana_out = await si.interviews_view((await UserExtra.find_one(UserExtra.name == "Ana")), "month", TODAY, "all", None, TODAY)
    assert "interviews.no_interviews" not in [t["id"] for t in ana_out["tips"]]


async def test_low_conversion_tip_in_view(db):
    (ana, ben, cy, dee), accs = await _team()
    await _bids(dee, 400, d=3)  # Dee: 500 bids, 1 interview -> 0.2 per 100 vs team median
    out = await si.interviews_view(dee, "month", TODAY, "all", None, TODAY)
    assert "interviews.low_conversion" in [t["id"] for t in out["tips"]]


async def test_held_round_results_matches_held_rounds(db):
    u = await _user("Ana")
    acc = await Account(name="A", createdBy=u.id, region="EU").insert()
    await _iv(u, acc, ("intro", 3, "passed"), ("tech_round_1", 4, "rejected", True), ("final", 5, "canceled"),
              ("ai_interview", 6, "passed"), ("panel", 7, "completed"), ("cultural", 8, "scheduled"))
    iv = await Interview.find_one()
    tz = app_tz()
    s, e = datetime(2026, 11, 1), datetime(2026, 11, 30)
    held = interview_rounds.held_rounds(iv, s, e, tz)
    res = interview_rounds.held_round_results(iv, s, e, tz)
    assert [(d, g, c is not None) for d, g, c, _ in held] == [(d, g, c) for d, g, c, _ in res]
    assert [r[3] for r in res] == ["passed", "rejected", "awaiting", "scheduled"]
    assert [r[1] for r in res] == ["intro", "tech", "panel", "hiring"]
