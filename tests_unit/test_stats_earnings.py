"""Statistics → Earnings view and rules (mongomock)."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")
os.environ["RESUME_QUEUE_DISABLED"] = "1"

from datetime import date, datetime

import pytest
from beanie import init_beanie
from mongomock_motor import AsyncMongoMockClient

from app.models import Transaction, UserExtra
from app.stats import earnings as se

pytestmark = [
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]

TODAY = date(2026, 11, 18)


@pytest.fixture
async def db():
    await init_beanie(database=AsyncMongoMockClient()["stats-earnings"], document_models=[UserExtra, Transaction])


async def _user(name, role="staff"):
    return await UserExtra(name=name, email=f"{name.lower()}@x.io", role=role).insert()


async def _tx(user, amount, y=2026, m=11, d=5, status="approved"):
    await Transaction(userId=user.id, date=datetime(y, m, d), amount=amount, status=status).insert()


async def _team(nets=(1000, 800, 600, 400)):
    users = [await _user(n) for n in ("Ana", "Ben", "Cy", "Dee")]
    for u, n in zip(users, nets):
        await _tx(u, n)
    return users


async def _view(u, period="month"):
    return await se.earnings_view(u, period, TODAY, "all", None, TODAY)


# ---- pure rules ----

def test_negative_period():
    tip = se.negative_period(-120)
    assert tip["severity"] == "bad" and "-$120" in tip["text"] and tip["impact"] == 120
    assert se.negative_period(0) is None


def test_below_and_above_own_average():
    below = se.below_own_average(700, 1000)
    assert below["severity"] == "watch" and "30% below" in below["text"] and "$1,000" in below["text"]
    assert se.below_own_average(800, 1000) is None and se.below_own_average(-5, 0) is None and se.below_own_average(5, None) is None
    above = se.above_own_average(1200, 1000)
    assert above["severity"] == "good"
    assert se.above_own_average(1100, 1000) is None and se.above_own_average(5, -10) is None


def test_no_earnings():
    assert se.no_earnings(False)["text"] == "No earnings recorded this period."
    assert se.no_earnings(True) is None


# ---- view ----

async def test_net_income_outcome_from_approved_only(db):
    ana, *_ = await _team()
    await _tx(ana, -300, d=6)
    await _tx(ana, 5000, d=7, status="pending")
    await _tx(ana, 5000, d=8, status="rejected")
    await _tx(ana, 900, m=12, d=1)  # outside the period
    out = await _view(ana)
    h = out["headline"][0]
    assert h["value"] == 700 and h["format"] == "usd" and h["note"] == "income $1,000 · expenses $300"
    row = next(r for r in out["everyone"]["rows"] if r["isSubject"])
    assert row["parts"] == {"income": 1000, "outcome": 300} and out["everyone"]["metric"] == "net"


async def test_negative_net_ranks_and_tips(db):
    ana, ben, *_ = await _team((-200, 800, 600, 400))
    out = await _view(ana)
    h = out["headline"][0]
    assert h["value"] == -200 and h["rank"] == 4 and h["of"] == 4
    assert out["notEnoughData"] is False
    assert out["tips"][0]["id"] == "earnings.negative_period"
    assert out["everyone"]["rows"][-1]["isSubject"] is True


async def test_no_transactions_would_rank_and_no_earnings_tip(db):
    await _team()
    zed = await _user("Zed")
    out = await _view(zed)
    h = out["headline"][0]
    assert h["value"] == 0 and h["of"] == 5 and h["rank"] == 5 and h["team"]["n"] == 4
    assert [t["id"] for t in out["tips"]] == ["earnings.no_earnings"]
    assert out["headline"][2]["value"] is None


async def test_own_average_three_previous_months(db):
    ana, *_ = await _team((600, 800, 600, 400))
    await _tx(ana, 1000, m=10)
    await _tx(ana, 500, m=9)
    await _tx(ana, 1500, m=8, d=31)
    await _tx(ana, 9999, m=7)  # fourth month back: ignored
    out = await _view(ana)
    avg = out["headline"][2]
    assert avg["value"] == 1000 and avg["key"] == "ownAvg" and avg["note"] == "this period −40% vs average"
    assert "earnings.below_own_average" in [t["id"] for t in out["tips"]]


async def test_week_and_custom_windows(db):
    assert se._previous_windows("week", date(2026, 11, 16), date(2026, 11, 22))[2] == (date(2026, 10, 26), date(2026, 11, 1))
    assert se._previous_windows("custom", date(2026, 11, 10), date(2026, 11, 14))[1] == (date(2026, 10, 31), date(2026, 11, 4))


async def test_below_and_above_in_view(db):
    ana, *_ = await _team((1500, 800, 600, 400))
    await _tx(ana, 1000, m=10)
    out = await _view(ana)
    assert "earnings.above_own_average" in [t["id"] for t in out["tips"]]
    ben = await _user("Eve")
    await _tx(ben, 5000, m=10)
    await _tx(ben, 100)
    out = await _view(ben)
    assert "earnings.below_own_average" in [t["id"] for t in out["tips"]]


async def test_history_but_nothing_this_period_only_no_earnings_tip(db):
    await _team()
    zed = await _user("Zed")
    for m in (8, 9, 10):
        await _tx(zed, 1000, m=m)
    out = await _view(zed)
    assert out["headline"][2]["value"] == 1000
    assert [t["id"] for t in out["tips"]] == ["earnings.no_earnings"]


async def test_own_average_january_year_wrap(db):
    ana, *_ = await _team()
    for y, m in ((2025, 12), (2025, 11), (2025, 10)):
        await _tx(ana, 300, y=y, m=m)
    await _tx(ana, 9999, y=2025, m=9)
    out = await se.earnings_view(ana, "month", date(2026, 1, 20), "all", None, date(2026, 1, 20))
    assert out["headline"][2]["value"] == 300


async def test_pending_rejected_previous_not_counted_and_zero_avg(db):
    ana, *_ = await _team()
    await _tx(ana, 900, m=10, status="pending")
    await _tx(ana, 900, m=9, status="rejected")
    assert (await _view(ana))["headline"][2]["value"] is None
    await _tx(ana, 100, m=10)
    await _tx(ana, -100, m=9)
    h = (await _view(ana))["headline"][2]
    assert h["value"] == 0 and "%" not in h["note"]


KEYS = {"tab", "label", "subject", "headline", "tips", "everyone", "trend", "notEnoughData", "definitions"}


async def test_contract_keys(db):
    ana, *_ = await _team()
    out = await _view(ana)
    assert set(out) == KEYS and out["tab"] == "earnings"
    assert [h["key"] for h in out["headline"]] == ["net", "rank", "ownAvg"]
    assert out["everyone"]["rowKind"] == "person" and len(out["definitions"]) == 2
    assert out["trend"]["buckets"][1]["you"] == 1000 and out["trend"]["buckets"][1]["median"] == 700


async def test_not_enough_data(db):
    ana = await _user("Ana")
    await _tx(ana, 100)
    assert (await _view(ana))["notEnoughData"] is True


async def test_new_starter_first_month_has_no_average_tip(db):
    ana, *_ = await _team((1500, 800, 600, 400))
    out = await _view(ana)  # first ever transaction is this period
    assert out["headline"][2]["value"] is None
    assert not [t for t in out["tips"] if t["id"].endswith("own_average")]


async def test_average_only_over_periods_since_first_transaction(db):
    ana, *_ = await _team((1500, 800, 600, 400))
    await _tx(ana, 1000, m=10)  # first transaction: October; Sep and Aug are before it
    out = await _view(ana)
    assert out["headline"][2]["value"] == 1000  # not 1000 / 3


def test_usd_under_a_cent():
    assert se._usd(0.004) == "<$0.01" and se._usd(-0.004) == "<$0.01" and se._usd(0) == "$0.00"
    assert se._usd(-0.5) == "-$0.50"
