"""Statistics → Profiles view and rules (mongomock)."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")
os.environ["RESUME_QUEUE_DISABLED"] = "1"

import itertools
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from beanie import init_beanie
from mongomock_motor import AsyncMongoMockClient

from app.models import Account, DailyPlan, Interview, InterviewQuestion, UserExtra
from app.models.bid import Bid
from app.models.bidder import Bidder
from app.models.weekly_plan import WeeklyPlan
from app.stats import profiles as sp

pytestmark = [
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]

TODAY = date(2026, 11, 18)
START, END = date(2026, 11, 1), date(2026, 11, 30)
_job = itertools.count()


# ---- pure rules ----

def _row(bids, ivs, archived=False, name="P"):
    return {"name": name, "region": "EU", "archived": archived, "bids": bids, "interviews": ivs}


def test_dead_profile_fires_at_150_bids_zero_interviews():
    tip = sp.dead_profile({"a": _row(150, 0, name="Zed"), "b": _row(400, 3, name="Ok")})
    assert tip["id"] == "profiles.dead_profile" and tip["severity"] == "bad" and "Zed" in tip["text"] and "Ok" not in tip["text"]
    assert tip["impact"] > 0


def test_dead_profile_not_below_150():
    assert sp.dead_profile({"a": _row(149, 0)}) is None
    assert sp.dead_profile({"a": _row(500, 1)}) is None


def test_dead_profile_names_up_to_three():
    tip = sp.dead_profile({str(i): _row(200, 0, name=f"P{i}") for i in range(5)})
    assert "P0" in tip["text"] and "P2" in tip["text"] and "P3" not in tip["text"] and "2 more" in tip["text"]


def test_archived_not_idle_or_dead():
    assert sp.dead_profile({"a": _row(500, 0, archived=True)}) is None


def test_region_gap_needs_30_bids_each_and_2x():
    tip = sp.region_gap({"EU": (100, 5), "US": (100, 2)})
    assert tip["severity"] == "watch" and "EU profiles convert 2.5× US (5.0 vs 2.0 per 100)" in tip["text"]
    assert sp.region_gap({"EU": (100, 5), "US": (29, 0)}) is None  # US under 30 bids: not compared
    assert sp.region_gap({"EU": (100, 5), "US": (100, 3)}) is None  # < 2x
    assert sp.region_gap({"EU": (100, 0), "US": (100, 0)}) is None  # best is 0
    assert sp.region_gap({"EU": (100, 5)}) is None


def test_region_gap_zero_worst():
    tip = sp.region_gap({"EU": (100, 5), "US": (100, 0)})
    assert tip is not None and "EU" in tip["text"] and "US" in tip["text"]


# ---- view ----

@pytest.fixture
async def db():
    await init_beanie(
        database=AsyncMongoMockClient()["stats-profiles"],
        document_models=[UserExtra, Account, DailyPlan, WeeklyPlan, Interview, InterviewQuestion, Bid, Bidder],
    )


async def _user(name, role="staff"):
    return await UserExtra(name=name, email=f"{name.lower()}@x.io", role=role).insert()


async def _acc(user, name, region="EU", archived=False):
    return await Account(name=name, createdBy=user.id, region=region, archived=archived).insert()


async def _self(user, acc, n, d=2):
    plan = await DailyPlan.find_one(DailyPlan.userId == user.id, DailyPlan.date == datetime(2026, 11, d))
    if plan:
        plan.done.bidsSelfByProfile[str(acc.id)] = plan.done.bidsSelfByProfile.get(str(acc.id), 0) + n
        await plan.save()
        return
    await DailyPlan(userId=user.id, date=datetime(2026, 11, d), year=2026, weekNumber=45,
                    done={"bidsSelf": n, "bidsSelfByProfile": {str(acc.id): n}}).insert()


async def _bidder(user, acc, n, day=10):
    b = await Bidder(createdBy=user.id, name=f"B{next(_job)}", profileId=acc.id, rate=1, screenshotFolderUrl="s3://b/").insert()
    first = datetime(2026, 11, day, 10, tzinfo=ZoneInfo("America/Chicago")).astimezone(timezone.utc).replace(tzinfo=None)
    for _ in range(n):
        await Bid(bidderId=b.id, ownerId=user.id, jobKey=f"k{next(_job)}", firstAt=first, status="approved", screenshots=[]).insert()


async def _iv(user, acc, day=5, status="passed", month=11):
    when = datetime(2026, month, day, 12)
    await Interview(accountId=acc.id, createdBy=user.id, scheduledAt=when, companyName="Acme",
                    stageHistory=[{"stage": "intro", "at": when, "scheduledAt": when, "status": status}]).insert()


async def _team():
    """Ana (EU 100 bids / 4 iv, US 100 / 1, old 20 archived / 0), Ben, Cy, Dee with one 100-bid EU profile each (3, 2, 1 interviews)."""
    ana = await _user("Ana")
    eu, us = await _acc(ana, "Ana EU"), await _acc(ana, "Ana US", "US")
    old = await _acc(ana, "Old", "EU", archived=True)
    await _self(ana, eu, 100)
    await _self(ana, us, 100, d=3)
    await _self(ana, old, 20, d=4)
    for _ in range(4):
        await _iv(ana, eu)
    await _iv(ana, us)
    others = [ana]
    for n, k in (("Ben", 3), ("Cy", 2), ("Dee", 1)):
        u = await _user(n)
        a = await _acc(u, f"{n} EU")
        await _self(u, a, 100)
        for _ in range(k):
            await _iv(u, a)
        others.append(u)
    return others, (eu, us, old)


async def test_profile_counts_self_bidder_interviews(db):
    ana = await _user("Ana")
    a = await _acc(ana, "Ana EU")
    await _self(ana, a, 12)
    await _bidder(ana, a, 8)
    await _iv(ana, a)
    await _iv(ana, a, month=10)  # outside the window
    out = await sp.profile_counts(ana.id, START, END, "all")
    assert out[str(a.id)] == {"name": "Ana EU", "region": "EU", "archived": False,
                              "bids": 20, "selfBids": 12, "bidderBids": 8, "interviews": 1}


async def test_region_filter_hides_other_regions(db):
    (ana, *_), _ = await _team()
    out = await sp.profile_counts(ana.id, START, END, "US")
    assert [v["name"] for v in out.values()] == ["Ana US"]
    view = await sp.profiles_view(ana, "month", TODAY, "US", None, TODAY)
    assert [r["name"] for r in view["everyone"]["rows"]] == ["Ana US"]


KEYS = {"tab", "label", "subject", "headline", "tips", "everyone", "trend", "notEnoughData", "definitions"}


async def test_contract_keys_rowkind_profile_trend_null(db):
    (ana, *_), _ = await _team()
    await _acc(ana, "Idle")  # non-archived, no bids: counts in the total only
    out = await sp.profiles_view(ana, "month", TODAY, "all", None, TODAY)
    assert set(out) == KEYS and out["tab"] == "profiles" and out["trend"] is None and len(out["definitions"]) == 3
    assert [h["key"] for h in out["headline"]] == ["best", "rank", "zero", "active"]
    best, rank, zero, active = out["headline"]
    assert best["value"] == 4.0 and best["format"] == "per100" and best["note"] == "Ana EU" and best["team"]["n"] == 4
    assert best["rank"] == 1 and best["of"] == 4 and best["higherIsBetter"] is True
    assert rank["value"] == 1 and rank["note"] == "of 4" and rank["team"] is None
    assert zero["value"] == 0 and zero["format"] == "count" and zero["higherIsBetter"] is False
    assert active["value"] == 2 and active["note"] == "3 in total" and active["team"]["n"] == 4
    ev = out["everyone"]
    assert ev["rowKind"] == "profile" and ev["metric"] == "per100"
    rows = {r["name"]: r for r in ev["rows"]}
    assert set(rows) == {"Ana EU", "Ana US", "Old"}  # archived with bids still listed
    eu = rows["Ana EU"]
    assert eu["value"] == 4.0 and eu["rank"] == 1 and eu["region"] == "EU" and eu["isSubject"] is False
    assert eu["parts"] == {"self": 100, "bidder": 0}
    assert rows["Ana US"]["value"] == 1.0 and rows["Ana US"]["rank"] == 2
    assert rows["Old"]["value"] is None and rows["Old"]["rank"] is None and rows["Old"]["vsMedian"] is None
    assert eu["vsMedian"] == pytest.approx(0.6)  # 4.0 vs median(4, 1) = 2.5


async def test_under_30_bids_profile_value_none(db):
    (ana, *_), _ = await _team()
    small = await _acc(ana, "Small")
    await _self(ana, small, 29, d=5)
    await _iv(ana, small)
    out = await sp.profiles_view(ana, "month", TODAY, "all", None, TODAY)
    row = next(r for r in out["everyone"]["rows"] if r["name"] == "Small")
    assert row["value"] is None and row["rank"] is None
    assert "profiles.dead_profile" not in [t["id"] for t in out["tips"]]


async def test_zero_profile_rank_lower_is_better(db):
    (ana, ben, *_), (eu, *_) = await _team()
    dud = await _acc(ben, "Ben dud")
    await _self(ben, dud, 60, d=6)  # Ben: 1 profile with 30+ bids and 0 interviews
    out = await sp.profiles_view(ana, "month", TODAY, "all", None, TODAY)
    zero = out["headline"][2]
    assert zero["value"] == 0 and zero["rank"] == 1 and zero["team"]["max"] == 1
    out = await sp.profiles_view(ben, "month", TODAY, "all", None, TODAY)
    assert out["headline"][2]["value"] == 1 and out["headline"][2]["rank"] == 4


async def test_no_profiles_empty_and_tip(db):
    (ana, *_), _ = await _team()
    nobody = await _user("Zed")
    out = await sp.profiles_view(nobody, "month", TODAY, "all", None, TODAY)
    assert out["everyone"]["rows"] == [] and out["headline"][0]["value"] is None
    assert out["headline"][1]["value"] is None and out["headline"][1]["note"] == "needs a profile with 30 bids"
    assert out["headline"][3]["value"] == 0 and out["headline"][3]["rank"] == 5 and out["headline"][3]["of"] == 5
    assert [t["id"] for t in out["tips"]] == ["profiles.no_profiles"]
    assert out["tips"][0]["severity"] == "bad" and out["tips"][0]["text"] == "No active profiles this period"


async def test_dead_and_region_gap_tips_in_view(db):
    (ana, *_), (eu, us, old) = await _team()
    dud = await _acc(ana, "Dud", "US")
    for d in (2, 3, 4, 5, 6):
        await _self(ana, dud, 40, d=d + 0)  # 28d window ending yesterday (Oct 21 - Nov 17)
    out = await sp.profiles_view(ana, "month", TODAY, "all", None, TODAY)
    ids = [t["id"] for t in out["tips"]]
    assert "profiles.dead_profile" in ids and "profiles.region_gap" in ids
    assert "bids.idle_profiles" in ids  # nothing in the last 7 days


async def test_not_enough_data(db):
    ana = await _user("Ana")
    a = await _acc(ana, "Ana EU")
    await _self(ana, a, 100)
    out = await sp.profiles_view(ana, "month", TODAY, "all", None, TODAY)
    assert out["notEnoughData"] is True and out["tips"] == []
