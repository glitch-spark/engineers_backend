"""Bucket regular daily plans into day / week / month totals for the dashboard chart.

Pure helpers (no Mongo) so the window and bucket math is unit-tested directly.
Weeks follow the app's calendar-year week (`app.alerts.app_week_key`), the same
week the Report filter and each plan's `weekNumber` use.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Iterable, Literal

from app.alerts import app_week_key

Bucket = Literal["day", "week", "month"]

# How many buckets the chart shows, ending with the one that holds `today`.
PERIODS: dict[str, int] = {"day": 14, "week": 12, "month": 12}

METRICS = ("bidsHandsOn", "bidsByBidder", "interviewsDone", "interviewsNew")


def bucket_key(day: date, bucket: Bucket) -> str:
    if bucket == "day":
        return day.isoformat()
    if bucket == "week":
        return app_week_key(datetime(day.year, day.month, day.day))[2]
    return f"{day.year}-{day.month:02d}"


def window_start(today: date, bucket: Bucket, periods: int) -> date:
    """First day of the oldest bucket in a `periods`-bucket window ending on `today`."""
    if bucket == "day":
        return today - timedelta(days=periods - 1)
    if bucket == "month":
        months = today.year * 12 + (today.month - 1) - (periods - 1)
        return date(months // 12, months % 12 + 1, 1)
    # App weeks restart on Jan 1, so step back day by day instead of 7 at a time.
    seen = {bucket_key(today, bucket)}
    day = today
    while True:
        prev = day - timedelta(days=1)
        key = bucket_key(prev, bucket)
        if key not in seen:
            if len(seen) == periods:
                return day
            seen.add(key)
        day = prev


def _day(value: Any) -> date:
    return value.date() if isinstance(value, datetime) else value


def build_series(plans: Iterable[Any], today: date, bucket: Bucket, periods: int) -> list[dict]:
    """One row per bucket, oldest first, with zeros for buckets that have no plans."""
    start = window_start(today, bucket, periods)
    rows: dict[str, dict] = {}
    day = start
    while day <= today:
        key = bucket_key(day, bucket)
        row = rows.get(key)
        if row is None:
            row = {"key": key, "bucketStart": day.isoformat(), "planCount": 0}
            row.update({m: 0 for m in METRICS})
            rows[key] = row
        row["bucketEnd"] = day.isoformat()
        day += timedelta(days=1)

    for plan in plans:
        plan_day = _day(plan.date)
        if plan_day < start or plan_day > today:
            continue
        row = rows[bucket_key(plan_day, bucket)]
        row["planCount"] += 1
        for m in METRICS:
            row[m] += int(getattr(plan, m, 0) or 0)
    return list(rows.values())


def sum_totals(series: list[dict], plans: Iterable[Any], start: date, today: date) -> dict:
    """Window totals, plus custom items summed by label (case-insensitive)."""
    totals: dict[str, Any] = {m: sum(r[m] for r in series) for m in METRICS}
    totals["planCount"] = sum(r["planCount"] for r in series)
    custom: dict[str, dict] = {}
    for plan in plans:
        if not start <= _day(plan.date) <= today:
            continue
        for item in getattr(plan, "customItems", None) or []:
            label = (item.label or "").strip()
            if not label:
                continue
            entry = custom.setdefault(label.lower(), {"label": label, "value": 0})
            entry["value"] += int(item.value or 0)
    totals["customItems"] = sorted(custom.values(), key=lambda e: (-e["value"], e["label"].lower()))
    return totals
