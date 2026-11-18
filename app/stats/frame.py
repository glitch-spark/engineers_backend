"""Pure helpers shared by every Statistics tab (you vs team)."""
from __future__ import annotations

from calendar import monthrange
from datetime import date, timedelta
from typing import Optional

from app import interview_rates

_SEVERITY = {"bad": 0, "watch": 1, "good": 2}
MAX_TIPS = 4


def _quantile(s: list[float], q: float) -> float:
    pos = (len(s) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def distribution(values: list[float]) -> dict:
    if not values:
        return {"median": None, "p25": None, "p75": None, "min": None, "max": None, "n": 0}
    s = sorted(values)
    return {"median": _quantile(s, 0.5), "p25": _quantile(s, 0.25), "p75": _quantile(s, 0.75),
            "min": s[0], "max": s[-1], "n": len(s)}


def rank_of(value: float, values: list[float], higher_is_better: bool = True, in_team: bool = True) -> tuple[int, int]:
    """1-based rank (ties share the best); a value outside the team gets its would-be rank among `len + 1`."""
    ahead = sum(1 for v in values if (v > value if higher_is_better else v < value))
    return ahead + 1, len(values) + (0 if in_team else 1)


def vs_median(value: float, median: Optional[float]) -> Optional[float]:
    return (value - median) / median if median else None


def working_days(start: date, end: date, today: date) -> list[date]:
    last = min(end, today)
    return [d for d in (start + timedelta(days=i) for i in range(max((last - start).days + 1, 0))) if d.weekday() < 5]


def previous_window(period: str, start: date, end: date, today: date) -> tuple[date, date]:
    """The same number of elapsed days immediately before: month = same day range of the previous month, week = same
    weekdays last week, custom = equal-length window right before."""
    n = max((min(end, today) - start).days + 1, 1)
    if period == "month":
        ps = date(start.year - (start.month == 1), start.month - 1 or 12, 1)
        return ps, min(ps + timedelta(days=n - 1), ps.replace(day=monthrange(ps.year, ps.month)[1]))
    if period == "week":
        ps = start - timedelta(days=7)
        return ps, ps + timedelta(days=n - 1)
    return start - timedelta(days=n), start - timedelta(days=1)


def trend_buckets(start: date, end: date) -> tuple[str, list[tuple[date, date, str]]]:
    unit = "week" if (end - start).days + 1 >= 14 else "day"
    return unit, interview_rates.buckets(unit, start, end)


def order_tips(tips: list[dict]) -> list[dict]:
    ordered = sorted(tips, key=lambda t: (_SEVERITY[t["severity"]], -t["impact"]))
    return [{k: v for k, v in t.items() if k != "impact"} for t in ordered[:MAX_TIPS]]
