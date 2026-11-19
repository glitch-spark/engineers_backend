"""The team response window: how many days after applying interviews arrive. Computed on read, cached 10 minutes.

Percentiles use nearest-rank: the value at 1-based rank ceil(p * n) of the sorted days (median p=0.5, p90 p=0.9)."""

import math
import time
from datetime import date, datetime, timedelta

from app.models import Application

WINDOW_DAYS = 90
MIN_MATCHED = 30  # fewer matched applications than this = thin data
FALLBACK_P90 = 14
MIN_MATURED = 7
_TTL = 600
_cache: dict[date, tuple[float, dict]] = {}


def clear_cache() -> None:
    _cache.clear()


def _rank(sorted_days: list[int], p: float) -> int:
    return sorted_days[math.ceil(p * len(sorted_days)) - 1]


async def response_window(today: date) -> dict:
    """{median, p90, matured_days, n, thin}; thin (< 30 matched in the last 90 days) = {None, 14}."""
    hit = _cache.get(today)
    if hit and time.monotonic() - hit[0] < _TTL:
        return hit[1]
    since = datetime(today.year, today.month, today.day) - timedelta(days=WINDOW_DAYS)
    # Bounded: only matched rows of the last 90 days, one integer each (index: interviewId / appliedAt).
    days = sorted([d["daysToInterview"] async for d in Application.get_motor_collection().find(
        {"interviewId": {"$ne": None}, "appliedAt": {"$gte": since}, "daysToInterview": {"$ne": None},
         "byInterviewOnly": {"$ne": True}},
        {"daysToInterview": 1})])
    n = len(days)
    if n < MIN_MATCHED:
        median, p90 = None, FALLBACK_P90
    else:
        median, p90 = _rank(days, 0.5), _rank(days, 0.9)
    out = {"median": median, "p90": p90, "matured_days": max(MIN_MATURED, p90), "n": n, "thin": n < MIN_MATCHED}
    _cache[today] = (time.monotonic(), out)
    return out
