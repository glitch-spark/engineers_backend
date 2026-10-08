"""Pure, DB-free helpers backing the `/dashboard` router.

No Mongo access here — everything is plain date/number math so it can be
unit-tested directly. Weeks are the app's Mon-Sat working week (counting
window Mon 00:00 -> Sun 23:59:59; a Sunday rolls into the week that just
ended, since Sunday is that week's last day).
"""

from __future__ import annotations

import calendar
import math
from datetime import date, datetime, timedelta, timezone
from typing import Literal
from zoneinfo import ZoneInfo

from app.config import get_settings

Bucket = Literal["day", "week", "month"]

_WEEKDAY_ABBR = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def app_tz() -> ZoneInfo:
    tz_name = (get_settings().APP_TIMEZONE or "").strip() or "UTC"
    try:
        return ZoneInfo(tz_name)
    except Exception:
        return ZoneInfo("UTC")


def local_day_start_utc(d: date, tz: ZoneInfo) -> datetime:
    """Naive UTC datetime of local midnight of `d`."""
    local_midnight = datetime(d.year, d.month, d.day, tzinfo=tz)
    return local_midnight.astimezone(timezone.utc).replace(tzinfo=None)


def week_window(today: date) -> tuple[date, date, date]:
    """(monday, saturday, sunday) of the Mon-Sun week containing `today`.

    A Sunday is the last day of its own Mon-Sun week, so it stays in that
    week rather than rolling forward into the next one.
    """
    monday = today - timedelta(days=today.weekday())
    return monday, monday + timedelta(days=5), monday + timedelta(days=6)


def pick_bucket(start: date, end: date) -> Bucket:
    n = (end - start).days + 1
    if n <= 62:
        return "day"
    if n <= 182:
        return "week"
    return "month"


def bucket_keys(start: date, end: date, bucket: Bucket) -> list[date]:
    if bucket == "day":
        keys: list[date] = []
        d = start
        while d <= end:
            if d.weekday() != 6:
                keys.append(d)
            d += timedelta(days=1)
        if not keys:
            # Range is Sunday-only (e.g. a single Sunday) — never drop it.
            return [start]
        return keys
    if bucket == "week":
        first_monday = start - timedelta(days=start.weekday())
        last_monday = end - timedelta(days=end.weekday())
        keys = []
        d = first_monday
        while d <= last_monday:
            keys.append(d)
            d += timedelta(days=7)
        return keys
    # month: 1st of each month from start's month through end's month.
    keys = []
    y, m = start.year, start.month
    while (y, m) <= (end.year, end.month):
        keys.append(date(y, m, 1))
        m += 1
        if m > 12:
            m = 1
            y += 1
    return keys


def bucket_for(d: date, bucket: Bucket, keys: list[date]) -> date:
    if bucket == "day":
        if d in keys:
            return d
        if d.weekday() == 6:
            prev_saturday = d - timedelta(days=1)
            if prev_saturday in keys:
                return prev_saturday
            return d + timedelta(days=1)
        return d
    if bucket == "week":
        return d - timedelta(days=d.weekday())
    return date(d.year, d.month, 1)


def bucket_label(key: date, bucket: Bucket, start: date, end: date) -> str:
    if bucket == "day":
        span = (end - start).days + 1
        if span <= 7:
            return _WEEKDAY_ABBR[key.weekday()]
        return f"{calendar.month_abbr[key.month]} {key.day}"
    if bucket == "week":
        return f"{calendar.month_abbr[key.month]} {key.day}"
    # month
    if start.year == end.year:
        return calendar.month_abbr[key.month]
    return f"{calendar.month_abbr[key.month]} {key.year % 100:02d}"


def pace(actual: int, target: int | None, today: date) -> dict | None:
    if not target:
        return None
    elapsed = min(today.weekday() + 1, 6)
    expected = round(target * elapsed / 6)
    behind_by = max(0, expected - actual)
    on_track = actual >= expected
    remaining = 6 - elapsed
    per_day_needed = math.ceil(max(0, target - actual) / remaining) if remaining > 0 else None
    return {
        "expected": expected,
        "behindBy": behind_by,
        "perDayNeeded": per_day_needed,
        "onTrack": on_track,
    }


def streak(plan_days: set[date], today: date, max_days: int = 365) -> int:
    def _prev_working(d: date) -> date:
        d -= timedelta(days=1)
        return d - timedelta(days=1) if d.weekday() == 6 else d

    cur = today if today.weekday() != 6 else today - timedelta(days=1)
    if cur not in plan_days:
        cur = _prev_working(cur)  # today not logged yet does not break it
    count = 0
    while cur in plan_days and count < max_days:
        count += 1
        cur = _prev_working(cur)
    return count


def percentile(mine: int, scores: list[int]) -> dict | None:
    n = len(scores)
    if n < 3 or mine == 0:
        return None
    rank = 1 + sum(1 for s in scores if s > mine)
    # Integer ceiling division avoids float error from `round-trip through /`
    # (e.g. rank/n*100 landing a hair above an exact multiple of 100/n).
    pct = -(-rank * 100 // n)
    if pct <= 50:
        return {"position": "top", "percent": pct}
    return {"position": "bottom", "percent": -(-(n - rank + 1) * 100 // n)}


def month_keys(today: date, n: int = 12) -> list[str]:
    keys: list[str] = []
    y, m = today.year, today.month
    for _ in range(n):
        keys.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m == 0:
            m = 12
            y -= 1
    keys.reverse()
    return keys


CompareRange = Literal["week", "month", "30d"]


def compare_window(today: date, range_: str) -> tuple[date, date]:
    """[start, end] calendar days for the "You vs team" card: the Mon-Sun week, the month to date, or the
    last 30 days ending today."""
    if range_ == "week":
        monday, _saturday, sunday = week_window(today)
        return monday, sunday
    if range_ == "month":
        return today.replace(day=1), today
    if range_ == "30d":
        return today - timedelta(days=29), today
    raise ValueError(f"unknown range: {range_}")


def rank_rows(rows: list[tuple]) -> list[dict]:
    """(id, name, value) rows sorted by value (high first, then name), ranked so ties share a rank (1, 2, 2, 4).
    A None value ("no data", e.g. no goal set) ranks after every number; all None rows share one rank."""
    ordered = sorted(rows, key=lambda r: (r[2] is None, -(r[2] or 0), (r[1] or "").lower()))
    out: list[dict] = []
    for i, (row_id, name, value) in enumerate(ordered):
        rank = out[-1]["rank"] if out and out[-1]["value"] == value else i + 1
        out.append({"id": row_id, "name": name, "value": value, "rank": rank})
    return out


def working_days(start: date, end: date) -> list[date]:
    """Mon-Sat days in [start, end]."""
    return [start + timedelta(days=i) for i in range((end - start).days + 1)
            if (start + timedelta(days=i)).weekday() != 6]


def prorated_goal(week_goals: dict[date, int], start: date, end: date) -> int | None:
    """Sum of each week's goal (keyed by its Monday) times the share of its six working days inside [start, end];
    None when that comes to 0 (no goal set)."""
    total = 0.0
    for monday, goal in week_goals.items():
        inside = working_days(max(monday, start), min(monday + timedelta(days=6), end))
        total += goal * len(inside) / 6
    return round(total) or None


def percent(done: float, goal: int | None) -> int | None:
    return round(done / goal * 100) if goal else None


def logging_ratio(logged_days: set[date], start: date, end: date, today: date) -> tuple[int, int]:
    """(days logged, working days) over [start, min(end, today)]; today counts only once it is logged."""
    days = [d for d in working_days(start, min(end, today)) if d != today or d in logged_days]
    return sum(1 for d in days if d in logged_days), len(days)
