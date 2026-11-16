"""Report windows for the bid reports: cutoffs, [start, end) UTC windows and period keys.

Every boundary is built as a local wall-clock time (datetime.combine with the report timezone)
and only then converted to UTC, so a day spanning a DST change is 23 or 25 hours long and the
cutoff always lands on the configured wall-clock time.
"""

from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Literal, Optional
from zoneinfo import ZoneInfo

from app.config import WEEKDAYS, Settings, get_settings

Kind = Literal["daily", "weekly"]


@dataclass(frozen=True)
class Rules:
    daily_at: time
    weekly_day: int  # Monday=0
    weekly_at: time


@dataclass(frozen=True)
class BidSchedule:
    # daily_at / weekly_day / weekly_at are the rules before switch_at (or the only rules when there is no switch).
    tz: ZoneInfo
    daily_at: time
    weekly_day: int  # Monday=0
    weekly_at: time
    switch_at: Optional[datetime] = None  # aware in tz; next_rules apply from here on
    next_rules: Optional[Rules] = None


def _parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def schedule_from_settings(s: Optional[Settings] = None) -> BidSchedule:
    s = s or get_settings()
    tz = ZoneInfo(s.BID_REPORT_TIMEZONE)
    switch_at: Optional[datetime] = None
    next_rules: Optional[Rules] = None
    if s.BID_SCHEDULE_SWITCH_AT:  # Settings guarantees the three NEXT values are set with it
        switch_at = datetime.fromisoformat(s.BID_SCHEDULE_SWITCH_AT).replace(tzinfo=tz)
        next_rules = Rules(
            daily_at=_parse_hhmm(s.BID_NEXT_DAILY_REPORT_TIME),
            weekly_day=WEEKDAYS.index(s.BID_NEXT_WEEKLY_REPORT_DAY),
            weekly_at=_parse_hhmm(s.BID_NEXT_WEEKLY_REPORT_TIME),
        )
    return BidSchedule(
        tz=tz,
        daily_at=_parse_hhmm(s.BID_DAILY_REPORT_TIME),
        weekly_day=WEEKDAYS.index(s.BID_WEEKLY_REPORT_DAY),
        weekly_at=_parse_hhmm(s.BID_WEEKLY_REPORT_TIME),
        switch_at=switch_at,
        next_rules=next_rules,
    )


def _at(kind: Kind, sched: BidSchedule) -> time:
    return sched.daily_at if kind == "daily" else sched.weekly_at


def latest_cutoff(kind: Kind, now: datetime, sched: BidSchedule) -> datetime:
    """Most recent cutoff at or before `now`, as an aware datetime in the report timezone."""
    local = now.astimezone(sched.tz)
    at = _at(kind, sched)
    days_back = 0 if kind == "daily" else (local.weekday() - sched.weekly_day) % 7
    cutoff = datetime.combine(local.date() - timedelta(days=days_back), at, tzinfo=sched.tz)
    if cutoff > now:
        cutoff = datetime.combine(cutoff.date() - timedelta(days=1 if kind == "daily" else 7), at, tzinfo=sched.tz)
    return cutoff


def window_for(kind: Kind, cutoff: datetime, sched: BidSchedule) -> tuple[datetime, datetime]:
    """UTC-aware [start, end) window ending at `cutoff`: one local day (daily) or seven (weekly) long."""
    local = cutoff.astimezone(sched.tz)
    start = datetime.combine(local.date() - timedelta(days=1 if kind == "daily" else 7), _at(kind, sched), tzinfo=sched.tz)
    end = datetime.combine(local.date(), _at(kind, sched), tzinfo=sched.tz)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def open_window(kind: Kind, now: datetime, sched: BidSchedule) -> tuple[datetime, datetime]:
    """The in-progress period: [latest cutoff in UTC, now]."""
    return latest_cutoff(kind, now, sched).astimezone(timezone.utc), now


def period_key(cutoff: datetime) -> str:
    """YYYY-MM-DD of the cutoff's local date (cutoff must be in the report timezone)."""
    return cutoff.date().isoformat()
