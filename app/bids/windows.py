"""Report windows for the bid reports: cutoffs, [start, end) UTC windows and period keys.

Every boundary is built as a local wall-clock time (datetime.combine with the report timezone)
and only then converted to UTC, so a day spanning a DST change is 23 or 25 hours long and the
cutoff always lands on the configured wall-clock time.

An optional switch (BidSchedule.switch_at) changes the rules once: cutoffs before it follow the old rules and
cutoffs after it the next rules, so the period straddling the switch is longer than usual. With short_week
(BID_SCHEDULE_SHORT_WEEK) the weekly period straddling it is instead shorter: [last old weekly cutoff, switch). With no
switch every function returns what it did before the switch existed.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
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
    short_week: bool = False  # weekly old era ends at the switch itself (a short pay week), not a week before it


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
        short_week=bool(s.BID_SCHEDULE_SHORT_WEEK),
    )


def _utc(dt: datetime) -> datetime:
    # Instants are compared in UTC: two datetimes sharing one ZoneInfo compare by wall clock, which is wrong
    # around a DST change. A naive time is refused rather than read as the server's local time.
    if dt.tzinfo is None:
        raise TypeError(f"an aware datetime is needed, got naive {dt.isoformat()}")
    return dt.astimezone(timezone.utc)


def _old_rules(sched: BidSchedule) -> Rules:
    return Rules(sched.daily_at, sched.weekly_day, sched.weekly_at)


def _at(kind: Kind, rules: Rules) -> time:
    return rules.daily_at if kind == "daily" else rules.weekly_at


def _latest(kind: Kind, now: datetime, tz: ZoneInfo, rules: Rules) -> datetime:
    """Most recent cutoff at or before `now` under one set of rules (the whole schedule when there is no switch)."""
    local = now.astimezone(tz)
    at = _at(kind, rules)
    days_back = 0 if kind == "daily" else (local.weekday() - rules.weekly_day) % 7
    cutoff = datetime.combine(local.date() - timedelta(days=days_back), at, tzinfo=tz)
    if _utc(cutoff) > _utc(now):
        cutoff = datetime.combine(cutoff.date() - timedelta(days=1 if kind == "daily" else 7), at, tzinfo=tz)
    return cutoff


def _next(kind: Kind, after: datetime, tz: ZoneInfo, rules: Rules) -> datetime:
    """First cutoff strictly after `after` under one set of rules: the one following `_latest`."""
    last = _latest(kind, after, tz, rules)
    return datetime.combine(last.date() + timedelta(days=1 if kind == "daily" else 7), _at(kind, rules), tzinfo=tz)


# With a switch, old-rule cutoffs exist only up to the old era's end and new-rule cutoffs only from the switch on:
#   weekly: old at or before switch - 7 days (no stub week before the switch), new at or after the switch;
#           with short_week, old at or before the switch itself (the stub [last old cutoff, switch) is a pay week);
#   daily:  old at or before the switch, new strictly after it.


def _old_era_end(kind: Kind, sched: BidSchedule) -> datetime:
    switch = sched.switch_at.astimezone(sched.tz)
    if kind == "daily" or sched.short_week:
        return switch
    return datetime.combine(switch.date() - timedelta(days=7), switch.time(), tzinfo=sched.tz)


def _in_old_era(kind: Kind, cutoff: datetime, sched: BidSchedule) -> bool:
    return _utc(cutoff) <= _utc(_old_era_end(kind, sched))


def _in_new_era(kind: Kind, cutoff: datetime, sched: BidSchedule) -> bool:
    c, switch = _utc(cutoff), _utc(sched.switch_at)
    return c >= switch if kind == "weekly" else c > switch


def latest_cutoff(kind: Kind, now: datetime, sched: BidSchedule) -> datetime:
    """Most recent cutoff at or before `now`, as an aware datetime in the report timezone."""
    if sched.switch_at is None:
        return _latest(kind, now, sched.tz, _old_rules(sched))
    new = _latest(kind, now, sched.tz, sched.next_rules)
    if _in_new_era(kind, new, sched):
        return new
    # No new-rule cutoff yet: the last old-rule one, taken no later than the old era's end.
    return _latest(kind, min(_utc(now), _utc(_old_era_end(kind, sched))), sched.tz, _old_rules(sched))


def next_cutoff(kind: Kind, after: datetime, sched: BidSchedule) -> datetime:
    """First cutoff strictly after `after`, as an aware datetime in the report timezone."""
    old = _next(kind, after, sched.tz, _old_rules(sched))
    if sched.switch_at is None or _in_old_era(kind, old, sched):
        return old
    # No old-rule cutoff left: the first new-rule one after `after` that is in the new era.
    floor = _utc(sched.switch_at) - (timedelta(microseconds=1) if kind == "weekly" else timedelta(0))
    return _next(kind, max(_utc(after), floor), sched.tz, sched.next_rules)


def window_for(kind: Kind, cutoff: datetime, sched: BidSchedule) -> tuple[datetime, datetime]:
    """UTC-aware [start, end) window ending at `cutoff` and starting at the cutoff before it: one local day
    (daily) or seven (weekly) long, except around the switch (longer, or shorter with short_week)."""
    end = _utc(cutoff)
    return _utc(latest_cutoff(kind, end - timedelta(microseconds=1), sched)), end


def daily_cutoff_on(day: date, sched: BidSchedule) -> datetime:
    """The daily cutoff whose local date is `day`, in the report timezone."""
    old = datetime.combine(day, sched.daily_at, tzinfo=sched.tz)
    if sched.switch_at is None or _in_old_era("daily", old, sched):
        return old
    return datetime.combine(day, sched.next_rules.daily_at, tzinfo=sched.tz)


def report_day(moment: datetime, sched: BidSchedule) -> date:
    """The report day `moment` counts on: the local date of the first daily cutoff after it, so a moment exactly
    on a cutoff counts on the next day. A naive `moment` is UTC (as Mongo returns it)."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return next_cutoff("daily", moment, sched).date()


def open_window(kind: Kind, now: datetime, sched: BidSchedule) -> tuple[datetime, datetime]:
    """The in-progress period: [latest cutoff in UTC, now]."""
    return latest_cutoff(kind, now, sched).astimezone(timezone.utc), now


def period_key(cutoff: datetime) -> str:
    """YYYY-MM-DD of the cutoff's local date (cutoff must be in the report timezone)."""
    return cutoff.date().isoformat()
