"""Profile → Metrics comparisons: this profile, the owner's other profiles and every owner's profiles, measured the
same fair way.

- The compared window starts no earlier than the launch day (`profile_metrics.since_date`): before it self bids
  weren't recorded per profile, and tailor scores were measured differently.
- A profile counts from its own creation day inside the window, so a young profile isn't diluted by weeks it didn't
  exist; one created after the window isn't there at all. Per week = per day x 7 (exact weeks, never rounded up, so
  a 2-day-old profile keeps the same pace as a 13-day-old one).
- Averages keep only profiles with at least one bid in the window (idle profiles would pull them toward zero).
- Volumes (per week) are means over those profiles; rates and scores are pooled, so a profile with 3 bids doesn't
  weigh like one with 300.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import date, timezone
from typing import Awaitable, Callable, Optional

from app import profile_metrics as pm
from app.dashboard_stats import app_tz
from app.interview_rates import rate
from app.models import Account

log = logging.getLogger(__name__)

COMPARE_KEYS = (
    "profiles", "days", "bids", "decided", "bidsPerWeek", "interviewsPerWeek", "tailoredResumesPerWeek", "interviewsPerBid",
    "passRate", "tailorScore", "matchScore",
)


async def compared_window(start: date, end: date) -> Optional[tuple[date, date]]:
    """[max(start, launch day), end]; None when the launch day comes after `end` (nothing to compare). No launch day
    recorded: the range as is."""
    since = await pm.since_date()
    first = max(start, since) if since else start
    return (first, end) if first <= end else None


def window_stats(
    created: date, window: tuple[date, date], daily: dict[date, dict], score_lists: dict, rounds: list[dict],
) -> Optional[dict]:
    """One profile's raw numbers over its days in the window (from its creation day on); None when it was created
    after the window. `daily`: day -> counts (`profile_metrics._COUNTS` keys) covering at least those days;
    `score_lists`: already cut to the window; `rounds`: `profile_rounds` records (filtered here by day; home
    assessments never count toward passed / rejected)."""
    first, last = max(window[0], created), window[1]
    if first > last:
        return None
    days = [c for d, c in daily.items() if first <= d <= last]
    mine = [r for r in rounds if first <= r["day"] <= last and r["type"] != "home_assessment"]
    return {
        "days": (last - first).days + 1,
        "bids": sum(c.get("selfBids", 0) + c.get("bidderBids", 0) for c in days),
        "interviews": sum(c.get("selfInterviews", 0) + c.get("callerInterviews", 0) for c in days),
        "tailored": score_lists.get("tailoredResumes", 0),
        "tailorScores": list(score_lists.get("tailored", [])),
        "matchScores": list(score_lists.get("match", [])),
        "passed": sum(1 for r in mine if r["result"] == "passed"),
        "rejected": sum(1 for r in mine if r["result"] == "rejected"),
    }


def _mean(values: list[float]) -> Optional[float]:
    return round(sum(values) / len(values), 1) if values else None


def _score(values: list[int]) -> dict:
    return {"avg": _mean(values), "n": len(values)}


def pool(stats: list[Optional[dict]], *, drop_idle: bool = True) -> Optional[dict]:
    """CompareStats (`COMPARE_KEYS`) of these `window_stats`: per-week values are means over the profiles, rates and
    scores pooled; `days` is the profiles' mean days in the window (one profile: its own, so the page can hold back
    a volume verdict on a profile younger than a week). Absent profiles (None) are skipped, idle ones (no bids) too unless `drop_idle` is False; None when
    nothing is left."""
    kept = [s for s in stats if s is not None and (s["bids"] > 0 or not drop_idle)]
    if not kept:
        return None
    bids = sum(s["bids"] for s in kept)
    per_week = lambda field: _mean([s[field] * 7 / s["days"] for s in kept])  # noqa: E731
    decided = sum(s["passed"] + s["rejected"] for s in kept)
    return {
        "profiles": len(kept),
        "days": round(sum(s["days"] for s in kept) / len(kept)),
        "bids": bids,
        "decided": decided,
        "bidsPerWeek": per_week("bids"),
        "interviewsPerWeek": per_week("interviews"),
        "tailoredResumesPerWeek": per_week("tailored"),
        "interviewsPerBid": round(sum(s["interviews"] for s in kept) / bids, 4) if bids else None,
        "passRate": rate(decided, sum(s["passed"] for s in kept)),
        "tailorScore": _score([v for s in kept for v in s["tailorScores"]]),
        "matchScore": _score([v for s in kept for v in s["matchScores"]]),
    }


def empty_stats() -> dict:
    """CompareStats with nobody to compare: zero counts, no rates or scores."""
    return {"profiles": 0, "days": 0, "bids": 0, "decided": 0, "bidsPerWeek": None, "interviewsPerWeek": None,
            "tailoredResumesPerWeek": None, "interviewsPerBid": None, "passRate": None,
            "tailorScore": {"avg": None, "n": 0}, "matchScore": {"avg": None, "n": 0}}


def created_day(account) -> date:
    """The local day a profile was created."""
    return account.createdAt.replace(tzinfo=timezone.utc).astimezone(app_tz()).date()


async def benchmark(window: tuple[date, date]) -> dict:
    """CompareStats of every owner's active profiles over the window ("All profiles" on the Metrics tab): no names
    or ids, only the pooled numbers. Active = not archived (as the Profiles status filter)."""
    accounts = await Account.find({"archived": {"$ne": True}}).project(pm.ProfileSpan).to_list()
    if not accounts:
        return empty_stats()
    sources = await pm.load_sources(accounts, *window)
    rounds = await pm.profile_rounds([a.id for a in accounts], *window)
    stats = []
    for a in accounts:
        key = str(a.id)
        daily, scores = pm.days_from(key, *window, sources)
        stats.append(window_stats(created_day(a), window, daily, scores, rounds.get(key, [])))
    return pool(stats) or empty_stats()


class BenchmarkCache:
    """Benchmarks by window, stale-while-revalidate: younger than `fresh_s` served as is; older (up to `keep_s`)
    served at once while one background task recomputes it (a failed refresh keeps the old value and is logged; the
    next request retries); missing or older than `keep_s` computed in the request, once per window however many
    requests wait for it. Each process (Railway service) keeps its own."""

    def __init__(self, compute: Callable[[tuple[date, date]], Awaitable[dict]], *, fresh_s: float = 900,
                 keep_s: float = 86400, clock: Callable[[], float] = time.monotonic):
        self._compute, self._fresh, self._keep, self._clock = compute, fresh_s, keep_s, clock
        self._entries: dict[tuple[date, date], tuple[float, dict]] = {}
        self._locks: dict[tuple[date, date], asyncio.Lock] = {}
        self._refreshing: dict[tuple[date, date], asyncio.Task] = {}
        # One compute at a time: each loads every owner's sources for seconds, and they share the API process.
        self._one_at_a_time = asyncio.Semaphore(1)

    async def _run(self, window) -> dict:
        async with self._one_at_a_time:
            return await self._compute(window)

    def clear(self) -> None:
        self._entries.clear()
        self._locks.clear()
        self._refreshing.clear()

    def _usable(self, window) -> Optional[tuple[float, dict]]:
        hit = self._entries.get(window)
        return hit if hit and self._clock() - hit[0] <= self._keep else None

    async def get(self, window: tuple[date, date]) -> dict:
        hit = self._usable(window)
        if hit:
            if self._clock() - hit[0] > self._fresh and window not in self._refreshing:
                self._refreshing[window] = asyncio.create_task(self._refresh(window))
            return hit[1]
        async with self._locks.setdefault(window, asyncio.Lock()):
            hit = self._usable(window)  # another request computed it while this one waited
            if hit:
                return hit[1]
            value = await self._run(window)
            now = self._clock()
            self._entries = {k: v for k, v in self._entries.items() if now - v[0] <= self._keep}
            self._entries[window] = (now, value)
            return value

    async def _refresh(self, window) -> None:
        try:
            self._entries[window] = (self._clock(), await self._run(window))
        except Exception:
            log.exception("profile metrics benchmark refresh failed for %s", window)
        finally:
            self._refreshing.pop(window, None)

    async def wait_refreshes(self) -> None:
        """Wait for the background refreshes running now (tests)."""
        await asyncio.gather(*list(self._refreshing.values()), return_exceptions=True)


benchmark_cache = BenchmarkCache(benchmark)
