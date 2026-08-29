"""Pure helpers for in-app alerts (no I/O). Safe to unit-test without Mongo."""

from __future__ import annotations

import calendar
import math
from datetime import date, datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

LEAD_DAYS = 3
WEEKLY_PLAN_WINDOW_HOURS = 6


def charge_title(description: Optional[str], last4: str) -> str:
    text = (description or "").strip()
    return text if text else f"Card ****{last4}"


def clamp_billing_date(year: int, month: int, billing_day: int) -> date:
    last = calendar.monthrange(year, month)[1]
    day = min(max(int(billing_day), 1), last)
    return date(year, month, day)


def next_due_date(today: date, billing_day: int) -> date:
    """Upcoming due date. If today is after this month's due day, roll to next month."""
    this = clamp_billing_date(today.year, today.month, billing_day)
    if today <= this:
        return this
    if today.month == 12:
        return clamp_billing_date(today.year + 1, 1, billing_day)
    return clamp_billing_date(today.year, today.month + 1, billing_day)


def in_reminder_window(today: date, billing_day: int, lead_days: int = LEAD_DAYS) -> bool:
    due = next_due_date(today, billing_day)
    start = due - timedelta(days=lead_days)
    return start <= today <= due


def period_key_for_due(due: date) -> str:
    return f"{due.year:04d}-{due.month:02d}"


def paid_for_period(last_paid_on: Optional[date], due: date) -> bool:
    if last_paid_on is None:
        return False
    return last_paid_on.year == due.year and last_paid_on.month == due.month


def days_until(due: date, today: date) -> int:
    return (due - today).days


def due_phrase(days: int) -> str:
    if days <= 0:
        return "today"
    if days == 1:
        return "tomorrow"
    return f"in {days} days"


def app_week_key(local_dt: datetime) -> tuple[int, int, str]:
    """Match engineers_app WeeklyPlan.getWeekInfo (not ISO week).

    JS: Math.ceil((days + startOfYear.getDay() + 1) / 7) with getDay() Sunday=0.
    """
    d = local_dt.date()
    year = d.year
    start = date(year, 1, 1)
    days = (d - start).days
    js_sunday0 = (start.weekday() + 1) % 7
    week = math.ceil((days + js_sunday0 + 1) / 7)
    return year, week, f"{year}-W{week:02d}"


def monday_morning_window(local_dt: datetime, window_hours: int = WEEKLY_PLAN_WINDOW_HOURS) -> bool:
    return local_dt.weekday() == 0 and 0 <= local_dt.hour < window_hours


def daily_bids_target(weekly_bids_target: int) -> int:
    if weekly_bids_target <= 0:
        return 0
    return math.ceil(weekly_bids_target / 5)


def local_now(tz: ZoneInfo, now: Optional[datetime] = None) -> datetime:
    base = now or datetime.now(timezone.utc)
    if base.tzinfo is None:
        base = base.replace(tzinfo=timezone.utc)
    return base.astimezone(tz)


def local_day_bounds_utc(local_dt: datetime) -> tuple[datetime, datetime]:
    """UTC-aware [local midnight, next local midnight) for counting today's rows."""
    start_local = local_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def format_amount(amount: float) -> str:
    return f"${abs(amount):,.2f}"


def card_renewal_href(*, last4: str, title: str, amount: float, card_label: Optional[str]) -> str:
    from urllib.parse import urlencode

    params = {
        "renew": "1",
        "last4": last4,
        "description": title,
        "amount": f"{amount:g}",
        "cycle": "monthly",
    }
    if card_label:
        params["label"] = card_label
    return f"/transactions?{urlencode(params)}"
