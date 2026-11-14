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
    assert (s["days"], s["bids"]) == (14, 8)
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


# ---- GET /accounts/{id}/metrics: the compare block ----------------------------------------------------------------

import itertools  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from zoneinfo import ZoneInfo  # noqa: E402

from beanie import init_beanie  # noqa: E402
from mongomock_motor import AsyncMongoMockClient  # noqa: E402

from app.models import (  # noqa: E402
    Account, Bid, Bidder, DailyPlan, Interview, InterviewQuestion, JobApplicationLog, ResumeApplication, UserExtra,
)

_job = itertools.count()


@pytest.fixture
async def db():
    await init_beanie(database=AsyncMongoMockClient()["profile-compare-unit"], document_models=[
        UserExtra, Account, DailyPlan, Interview, InterviewQuestion, Bid, Bidder, JobApplicationLog, ResumeApplication])


@pytest.fixture
def today(monkeypatch):
    from app.routers import accounts

    monkeypatch.setattr(accounts, "_today", lambda: date(2026, 11, 13))


async def _profile(owner, name, created=datetime(2026, 1, 5, 15)):
    return await Account(name=name, region="US", createdBy=owner.id, createdAt=created).insert()


async def _bids(owner, acc, month, day, n):
    """n bidder bids at 10:00 Chicago (before the 18:30 cutoff, so they count on that day)."""
    b = await Bidder(createdBy=owner.id, name=f"B{next(_job)}", profileId=acc.id, rate=1,
                     screenshotFolderUrl="s3://b/").insert()
    first = datetime(2026, month, day, 10, tzinfo=ZoneInfo("America/Chicago")).astimezone(timezone.utc).replace(tzinfo=None)
    for _ in range(n):
        await Bid(bidderId=b.id, ownerId=owner.id, jobKey=f"k{next(_job)}", firstAt=first, status="approved",
                  screenshots=[]).insert()


async def _round(owner, acc, month, day, stage="intro", status="passed"):
    when = datetime(2026, month, day, 15)
    await Interview(accountId=acc.id, createdBy=owner.id, scheduledAt=when, companyName="Acme",
                    stageHistory=[{"stage": stage, "at": when, "scheduledAt": when, "status": status}]).insert()


async def _metrics(acc, owner, period="month", day="2026-11-13", to=""):
    from app.routers.accounts import get_profile_metrics

    return await get_profile_metrics(str(acc.id), owner, period=period, date=day, to=to)


async def test_compare_starts_at_launch_day(db, today, monkeypatch):
    launch(monkeypatch, "2026-11-05")
    ana = await UserExtra(name="Ana", email="ana@x.io").insert()
    a = await _profile(ana, "A")
    await _round(ana, a, 10, 20)
    await _round(ana, a, 11, 6)
    await _bids(ana, a, 11, 6, 40)
    out = await _metrics(a, ana, period="all", day="")
    assert out["comparedFrom"] == "2026-11-05"
    assert out["compare"]["this"]["interviewsPerBid"] == 0.025   # 1 / 40: the Oct 20 round isn't compared
    assert out["compare"]["this"]["decided"] == 1
    assert out["interviews"] == 2                                 # totals keep the full range
    assert tuple(out["compare"]["this"]) == pc.COMPARE_KEYS


async def test_compare_peers_leave_out_idle_and_unborn_profiles(db, today):
    ana = await UserExtra(name="Ana", email="ana@x.io").insert()
    a, b, c = await _profile(ana, "A"), await _profile(ana, "B"), await _profile(ana, "C")
    await _profile(ana, "D", created=datetime(2026, 11, 20, 15))  # after the window (it ends today, Nov 13)
    await _bids(ana, a, 11, 3, 10)
    await _bids(ana, b, 11, 3, 30)
    await _round(ana, b, 11, 4)
    out = await _metrics(a, ana)
    peers = out["compare"]["peers"]
    assert (peers["profiles"], peers["bids"], peers["interviewsPerBid"]) == (1, 30, 0.0333)
    assert out["compare"]["this"]["bids"] == 10
    assert out["peers"] is not None and "bids" in out["peers"]    # the old block is still there for older frontends
    del c


async def test_single_profile_owner_has_no_peers(db, today):
    ana = await UserExtra(name="Ana", email="ana@x.io").insert()
    a = await _profile(ana, "A")
    out = await _metrics(a, ana)
    assert out["compare"]["peers"] is None
    assert out["compare"]["this"]["bids"] == 0 and out["compare"]["this"]["interviewsPerBid"] is None


async def test_range_before_launch_has_no_comparison(db, today, monkeypatch):
    launch(monkeypatch, "2026-11-05")
    ana = await UserExtra(name="Ana", email="ana@x.io").insert()
    a = await _profile(ana, "A")
    await _bids(ana, a, 10, 10, 5)
    out = await _metrics(a, ana, period="custom", day="2026-10-01", to="2026-10-20")
    assert out["comparedFrom"] is None
    assert out["compare"] == {"this": None, "peers": None}
    assert out["bids"] == 5


def test_per_week_uses_exact_weeks_so_young_profiles_keep_pace():
    window = (date(2026, 11, 1), date(2026, 11, 13))
    ten_a_day = lambda first: daily({first + timedelta(days=i): {"bidderBids": 10} for i in range((window[1] - first).days + 1)}, *window)  # noqa: E731
    young = pc.window_stats(date(2026, 11, 12), window, ten_a_day(date(2026, 11, 12)), scores(), [])
    old = pc.window_stats(date(2026, 1, 1), window, ten_a_day(window[0]), scores(), [])
    assert (young["days"], old["days"]) == (2, 13)
    assert pc.pool([young], drop_idle=False)["bidsPerWeek"] == pc.pool([old], drop_idle=False)["bidsPerWeek"] == 70.0
    assert pc.pool([young], drop_idle=False)["days"] == 2
