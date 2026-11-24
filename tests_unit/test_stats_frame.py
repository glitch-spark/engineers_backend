from datetime import date, datetime

import pytest
from beanie import init_beanie
from mongomock_motor import AsyncMongoMockClient

from app import interview_rates as ir
from app.models import Account, DailyPlan, Interview, InterviewQuestion, UserExtra
from app.models.bid import Bid
from app.models.bidder import Bidder
from app.stats.frame import (
    distribution, order_tips, previous_window, rank_of, trend_buckets, vs_median, working_days,
)

pytestmark = [
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]
TODAY = date(2026, 11, 18)  # Wednesday


def test_distribution_quartiles():
    assert distribution([1, 2, 3, 4, 5]) == {"median": 3, "p25": 2, "p75": 4, "min": 1, "max": 5, "n": 5}
    assert distribution([4, 1])["median"] == 2.5
    assert distribution([7]) == {"median": 7, "p25": 7, "p75": 7, "min": 7, "max": 7, "n": 1}
    assert distribution([]) == {"median": None, "p25": None, "p75": None, "min": None, "max": None, "n": 0}


def test_rank_ties_share():
    assert rank_of(5, [9, 5, 5, 1]) == (2, 4)
    assert rank_of(5, [9, 5, 5, 1], higher_is_better=False) == (2, 4)
    assert rank_of(9, [9, 5, 5, 1], higher_is_better=False) == (4, 4)


def test_rank_outside_team():
    assert rank_of(0, [9, 5, 1], in_team=False) == (4, 4)


def test_vs_median_zero_median_is_none():
    assert vs_median(5, 0) is None and vs_median(5, None) is None
    assert vs_median(15, 10) == 0.5


def test_vs_median_negative_median_keeps_sign():
    assert vs_median(200, -100) == 3.0 and vs_median(-150, -100) == -0.5


def test_working_days_stop_at_today():
    assert working_days(date(2026, 11, 16), date(2026, 11, 22), TODAY) == [date(2026, 11, d) for d in (16, 17, 18)]
    assert working_days(date(2026, 11, 14), date(2026, 11, 15), TODAY) == [date(2026, 11, 14)]  # Sat yes, Sun no


def test_previous_window_month_same_days():
    assert previous_window("month", date(2026, 11, 1), date(2026, 11, 30), TODAY) == (date(2026, 10, 1), date(2026, 10, 18))


def test_previous_window_week():
    assert previous_window("week", date(2026, 11, 16), date(2026, 11, 22), TODAY) == (date(2026, 11, 9), date(2026, 11, 11))


def test_previous_window_custom():
    assert previous_window("custom", date(2026, 11, 5), date(2026, 11, 11), TODAY) == (date(2026, 10, 29), date(2026, 11, 4))


def test_trend_unit():
    assert trend_buckets(date(2026, 11, 16), date(2026, 11, 22))[0] == "day"
    unit, b = trend_buckets(date(2026, 11, 1), date(2026, 11, 30))
    assert unit == "week" and b == ir.buckets("week", date(2026, 11, 1), date(2026, 11, 30))


def test_order_tips_caps_at_four():
    tips = [{"id": str(i), "severity": s, "impact": i} for i, s in
            enumerate(["good", "watch", "bad", "bad", "watch", "good"])]
    out = order_tips(tips)
    assert [t["id"] for t in out] == ["3", "2", "4", "1"]
    assert all("impact" not in t for t in out)


@pytest.fixture
async def db():
    await init_beanie(database=AsyncMongoMockClient()["stats-frame-unit"],
                      document_models=[UserExtra, Account, DailyPlan, Interview, InterviewQuestion, Bid, Bidder])


async def test_counts_logged_only_false_includes_unposted_days(db):
    u = await UserExtra(name="Ana", email="ana@x.io").insert()
    d = date(2026, 11, 17)
    await DailyPlan(userId=u.id, date=datetime(2026, 11, 17), year=2026, weekNumber=47,
                    done={"bidsSelf": 7}, loggedAt=None).insert()
    assert (await ir.counts([u.id], d, d, "all", {}))[u.id][0] == 0
    assert (await ir.counts([u.id], d, d, "all", {}, logged_only=False))[u.id][0] == 7
