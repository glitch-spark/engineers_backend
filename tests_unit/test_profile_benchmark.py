"""Profile -> Metrics "All profiles": every owner's active profiles compared the fair way (app.profile_compare), behind
a stale-while-revalidate cache (mongomock-motor for the endpoint)."""
import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")
os.environ["RESUME_QUEUE_DISABLED"] = "1"

import asyncio
import itertools
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from beanie import init_beanie
from fastapi import HTTPException
from mongomock_motor import AsyncMongoMockClient

from app import profile_compare as pc
from app import profile_metrics as pm
from app.config import get_settings
from app.models import (
    Account, Bid, Bidder, DailyPlan, Interview, InterviewQuestion, JobApplicationLog, ResumeApplication, UserExtra,
)

pytestmark = [
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]

_job = itertools.count()
NOV = (date(2026, 11, 1), date(2026, 11, 13))


@pytest.fixture
async def db():
    await init_beanie(database=AsyncMongoMockClient()["profile-benchmark-unit"], document_models=[
        UserExtra, Account, DailyPlan, Interview, InterviewQuestion, Bid, Bidder, JobApplicationLog, ResumeApplication])


@pytest.fixture
def today(monkeypatch):
    from app.routers import accounts

    monkeypatch.setattr(accounts, "_today", lambda: date(2026, 11, 13))
    pc.benchmark_cache.clear()
    yield
    pc.benchmark_cache.clear()


def launch(monkeypatch, day: str):
    monkeypatch.setattr(pm, "get_settings", lambda: get_settings().model_copy(update={"PROFILE_METRICS_SINCE": day}))


async def _profile(owner, name, archived=False):
    return await Account(name=name, region="US", createdBy=owner.id, createdAt=datetime(2026, 1, 5, 15),
                         archived=archived).insert()


async def _bids(owner, acc, day, n):
    b = await Bidder(createdBy=owner.id, name=f"B{next(_job)}", profileId=acc.id, rate=1,
                     screenshotFolderUrl="s3://b/").insert()
    first = datetime(2026, 11, day, 10, tzinfo=ZoneInfo("America/Chicago")).astimezone(timezone.utc).replace(tzinfo=None)
    for _ in range(n):
        await Bid(bidderId=b.id, ownerId=owner.id, jobKey=f"k{next(_job)}", firstAt=first, status="approved",
                  screenshots=[]).insert()


async def _round(owner, acc, day, status="passed"):
    when = datetime(2026, 11, day, 15)
    await Interview(accountId=acc.id, createdBy=owner.id, scheduledAt=when, companyName="Acme",
                    stageHistory=[{"stage": "intro", "at": when, "scheduledAt": when, "status": status}]).insert()


async def _two_owners():
    ana = await UserExtra(name="Ana", email="ana@x.io").insert()
    bo = await UserExtra(name="Bo", email="bo@x.io").insert()
    a, b, _c = await _profile(ana, "A"), await _profile(bo, "B"), await _profile(bo, "C")  # C: idle
    gone = await _profile(bo, "Gone", archived=True)
    await _bids(ana, a, 3, 40)
    await _round(ana, a, 4)
    await _round(ana, a, 5, "rejected")
    await _bids(bo, b, 3, 60)
    await _round(bo, b, 6)
    await _bids(bo, gone, 3, 500)
    return ana, bo


async def _bench(user, period="month", day="2026-11-13", to=""):
    from app.routers.accounts import get_metrics_benchmark

    return await get_metrics_benchmark(user, period=period, date=day, to=to)


async def test_benchmark_pools_every_owner(db, today):
    ana, _bo = await _two_owners()
    out = await _bench(ana)
    assert (out["profiles"], out["bids"], out["interviewsPerBid"]) == (2, 100, 0.03)
    assert (out["decided"], out["passRate"]) == (3, 0.6667)
    assert set(out) == {"start", "end", "label", "comparedFrom", *pc.COMPARE_KEYS, "regionTops"}
    assert (out["start"], out["end"], out["comparedFrom"]) == ("2026-11-01", "2026-11-13", "2026-11-01")


async def test_benchmark_open_to_any_signed_in_user(db, today):
    await _two_owners()
    cy = await UserExtra(name="Cy", email="cy@x.io").insert()   # owns no profile
    out = await _bench(cy)
    assert out["profiles"] == 2 and out["bids"] == 100


async def test_benchmark_bad_period_is_422(db, today):
    ana = await UserExtra(name="Ana", email="ana@x.io").insert()
    with pytest.raises(HTTPException) as e:
        await _bench(ana, period="year")
    assert e.value.status_code == 422


async def test_benchmark_all_time_starts_at_launch(db, today, monkeypatch):
    launch(monkeypatch, "2026-11-05")
    ana, _bo = await _two_owners()
    out = await _bench(ana, period="all", day="")
    assert (out["start"], out["comparedFrom"], out["label"]) == ("2026-11-05", "2026-11-05", "All time")
    assert out["bids"] == 0 and out["profiles"] == 0 and out["tailorScore"] == {"avg": None, "n": 0}


async def test_benchmark_range_before_launch_is_empty(db, today, monkeypatch):
    launch(monkeypatch, "2026-11-05")
    ana, _bo = await _two_owners()
    out = await _bench(ana, period="custom", day="2026-10-01", to="2026-10-20")
    assert out["comparedFrom"] is None and out["profiles"] == 0 and out["interviewsPerBid"] is None


# ---- the cache ------------------------------------------------------------------------------------------------------

class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def _counting(fail_after=None):
    calls = []

    async def compute(window):
        calls.append(window)
        if fail_after is not None and len(calls) > fail_after:
            raise RuntimeError("db hiccup")
        await asyncio.sleep(0)
        return {"n": len(calls)}

    return compute, calls


async def test_cache_serves_fresh_entry_without_recomputing():
    compute, calls = _counting()
    clock = Clock()
    cache = pc.BenchmarkCache(compute, clock=clock)
    first = await cache.get(NOV)
    clock.t = 100
    assert await cache.get(NOV) is first and len(calls) == 1


async def test_cache_serves_stale_then_refreshes():
    compute, calls = _counting()
    clock = Clock()
    cache = pc.BenchmarkCache(compute, clock=clock)
    await cache.get(NOV)
    clock.t = 1000
    assert await cache.get(NOV) == {"n": 1}          # stale: served at once
    await cache.wait_refreshes()
    assert len(calls) == 2 and await cache.get(NOV) == {"n": 2}


async def test_cache_keeps_stale_value_when_refresh_fails():
    compute, calls = _counting(fail_after=1)
    clock = Clock()
    cache = pc.BenchmarkCache(compute, clock=clock)
    await cache.get(NOV)
    clock.t = 1000
    assert await cache.get(NOV) == {"n": 1}
    await cache.wait_refreshes()
    assert await cache.get(NOV) == {"n": 1}          # still served; this get retried in the background
    await cache.wait_refreshes()
    assert len(calls) == 3


async def test_cache_drops_entries_older_than_a_day():
    compute, calls = _counting()
    clock = Clock()
    cache = pc.BenchmarkCache(compute, clock=clock)
    await cache.get(NOV)
    clock.t = 90_000
    assert await cache.get(NOV) == {"n": 2} and len(calls) == 2


async def test_concurrent_misses_compute_once():
    compute, calls = _counting()
    cache = pc.BenchmarkCache(compute, clock=Clock())
    a, b = await asyncio.gather(cache.get(NOV), cache.get(NOV))
    assert a is b and len(calls) == 1


async def test_computes_run_one_at_a_time():
    active, peak = 0, 0

    async def compute(window):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return {"w": window}

    cache = pc.BenchmarkCache(compute, clock=Clock())
    other = (date(2026, 10, 1), date(2026, 10, 31))
    await asyncio.gather(cache.get(NOV), cache.get(other))
    assert peak == 1


async def test_peek_returns_none_and_warms_the_window():
    compute, calls = _counting()
    cache = pc.BenchmarkCache(compute, clock=Clock())
    assert cache.peek(NOV) is None
    assert cache.peek(NOV) is None                    # one warm-up already running: not a second
    await cache.wait_refreshes()
    assert cache.peek(NOV) == {"n": 1} and len(calls) == 1


async def test_peek_serves_stale_and_refreshes():
    compute, calls = _counting()
    clock = Clock()
    cache = pc.BenchmarkCache(compute, clock=clock)
    await cache.get(NOV)
    clock.t = 1000
    assert cache.peek(NOV) == {"n": 1}
    await cache.wait_refreshes()
    assert cache.peek(NOV) == {"n": 2} and len(calls) == 2
