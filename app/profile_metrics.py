"""Profile metrics: a profile's weekly bids, interviews and scores over its last completed weeks.

Weeks run Monday to Sunday and only completed ones count. A profile is measured from the latest of the window's
first Monday, the first Monday on or after PROFILE_METRICS_SINCE and the Monday of the week it was created.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from app.bids.windows import BidSchedule
from app.config import get_settings

WEEK_CHOICES = (4, 8, 12)


def since_date() -> date:
    """PROFILE_METRICS_SINCE as a date; date.min when unset."""
    s = get_settings().PROFILE_METRICS_SINCE
    return date.fromisoformat(s) if s else date.min


def span(weeks: int, today: date, since: date, created: date) -> tuple[date, date, int]:
    """(first Monday, last completed Sunday, weeks between them); 0 weeks when the start is after the end."""
    this_monday = today - timedelta(days=today.weekday())
    end = this_monday - timedelta(days=1)
    start = max(
        this_monday - timedelta(weeks=weeks),
        since + timedelta(days=-since.weekday() % 7),
        created - timedelta(days=created.weekday()),
    )
    return start, end, max(0, ((end - start).days + 1) // 7)


def bid_report_day(first_at: datetime, sched: BidSchedule) -> date:
    """The local date of the Bid Track daily window a naive-UTC time falls in (a bid at the cutoff is the next day's)."""
    local = first_at.replace(tzinfo=timezone.utc).astimezone(sched.tz)
    return local.date() + timedelta(days=1 if local.time() >= sched.daily_at else 0)
