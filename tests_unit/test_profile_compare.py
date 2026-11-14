"""Profile → Metrics comparisons: a profile measured over the compared window (from the launch day, from its own
creation), averages of bidding profiles only, rates and scores pooled (mongomock for the endpoint tests)."""
import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")
os.environ["RESUME_QUEUE_DISABLED"] = "1"

from datetime import date, timedelta

import pytest

from app import profile_metrics as pm
from app import profile_compare as pc
from app.config import get_settings

pytestmark = [
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]

NOV = (date(2026, 11, 1), date(2026, 11, 28))


def launch(monkeypatch, day: str):
    monkeypatch.setattr(pm, "get_settings", lambda: get_settings().model_copy(update={"PROFILE_METRICS_SINCE": day}))


def daily(per_day: dict[date, dict], first=NOV[0], last=NOV[1]) -> dict:
    """Every day of [first, last] with zero counts, then the given ones."""
    out = {first + timedelta(days=i): {"selfBids": 0, "bidderBids": 0, "selfInterviews": 0, "callerInterviews": 0}
           for i in range((last - first).days + 1)}
    for d, c in per_day.items():
        out[d].update(c)
    return out


def scores(tailored=(), match=(), n=None):
    return {"tailored": list(tailored), "match": list(match), "tailoredResumes": len(tailored) if n is None else n}


def stats(created=date(2026, 1, 1), bids=0, interviews=0, rounds=(), tailored=(), match=(), window=NOV):
    d = daily({window[0]: {"bidderBids": bids, "selfInterviews": interviews}}, *window)
    return pc.window_stats(created, window, d, scores(tailored, match), list(rounds))


def rd(result, kind="intro", day=date(2026, 11, 3), caller=False):
    return {"type": kind, "result": result, "caller": caller, "day": day}


def test_week_count_starts_at_creation():
    d = daily({date(2026, 11, 16): {"bidderBids": 8}})
    s = pc.window_stats(date(2026, 11, 15), NOV, d, scores(), [])
    assert (s["weeks"], s["bids"]) == (2, 8)
    assert pc.pool([s])["bidsPerWeek"] == 4.0


def test_days_before_creation_are_not_counted():
    d = daily({date(2026, 11, 3): {"bidderBids": 5}, date(2026, 11, 20): {"bidderBids": 2}})
    assert pc.window_stats(date(2026, 11, 15), NOV, d, scores(), [rd("passed")])["bids"] == 2


def test_created_after_window_is_absent():
    assert pc.window_stats(date(2026, 12, 1), NOV, daily({}), scores(), []) is None


def test_rounds_outside_window_and_home_assessments_left_out():
    rounds = [rd("passed"), rd("rejected", day=date(2026, 10, 30)), rd("rejected", kind="home_assessment"),
              rd("awaiting")]
    s = stats(bids=10, rounds=rounds)
    assert (s["passed"], s["rejected"]) == (1, 0)


def test_idle_profiles_are_dropped_from_averages():
    out = pc.pool([stats(bids=40, window=(date(2026, 11, 1), date(2026, 11, 14))),
                   stats(bids=0, window=(date(2026, 11, 1), date(2026, 11, 14)))])
    assert (out["profiles"], out["bids"], out["bidsPerWeek"]) == (1, 40, 20.0)


def test_rates_and_scores_are_pooled():
    a = stats(bids=3, interviews=1, tailored=[90], rounds=[rd("passed")])
    b = stats(bids=97, interviews=1, tailored=[70, 70, 70], match=[60], rounds=[rd("rejected")] * 3)
    out = pc.pool([a, b])
    assert out["interviewsPerBid"] == 0.02            # 2 / 100, not the mean of 0.3333 and 0.0103
    assert out["tailorScore"] == {"avg": 75.0, "n": 4}
    assert out["matchScore"] == {"avg": 60.0, "n": 1}
    assert (out["passRate"], out["decided"]) == (0.25, 4)
    assert tuple(out) == pc.COMPARE_KEYS


def test_this_profile_kept_with_no_bids():
    out = pc.pool([stats(bids=0, tailored=[80, 80])], drop_idle=False)
    assert (out["profiles"], out["bidsPerWeek"], out["interviewsPerBid"]) == (1, 0.0, None)
    assert out["tailoredResumesPerWeek"] == 0.5 and out["passRate"] is None


def test_pool_of_nothing_is_none():
    assert pc.pool([]) is None
    assert pc.pool([stats(bids=0)]) is None


async def test_compared_window_clamps_to_launch(monkeypatch):
    launch(monkeypatch, "2026-11-05")
    assert await pc.compared_window(date(2026, 10, 1), date(2026, 11, 13)) == (date(2026, 11, 5), date(2026, 11, 13))
    assert await pc.compared_window(date(2026, 10, 1), date(2026, 10, 20)) is None
    assert await pc.compared_window(date(2026, 11, 6), date(2026, 11, 9)) == (date(2026, 11, 6), date(2026, 11, 9))
