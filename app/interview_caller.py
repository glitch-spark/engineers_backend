"""Caller mode for interviews: a coworker joins the call at an exact time."""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from typing import Any, List, Optional
from zoneinfo import ZoneInfo

from beanie import PydanticObjectId
from pydantic import BaseModel

from app.models.interview import InterviewCaller
from app.slack_timezones import normalize_slack_timezone, validate_timezone

CALLER_METHODS = ("video", "phone_hushed", "phone_slynumber")


class CallerIn(BaseModel):
    enabled: bool = False
    callerName: Optional[str] = None
    # Local wall-clock start "HH:MM" in `timezone`; empty means TBD.
    time: Optional[str] = None
    # Calendar day "YYYY-MM-DD" as picked in the form; falls back to scheduledAt.
    date: Optional[str] = None
    timezone: Optional[str] = None
    method: Optional[str] = None
    methodValue: Optional[str] = None
    coworkerIds: Optional[List[str]] = None


class CallerError(ValueError):
    pass


def parse_hhmm(value: Optional[str]) -> Optional[time]:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.strptime(raw[:5], "%H:%M")
    except ValueError as exc:
        raise CallerError("invalid caller time; use HH:MM") from exc
    return parsed.time()


def parse_day(value: Optional[str]) -> Optional[date]:
    raw = (value or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError as exc:
        raise CallerError("invalid caller date; use YYYY-MM-DD") from exc


def local_start_utc(day: date, at: time, tz_name: str) -> datetime:
    """Wall-clock `day at` in `tz_name`, converted to aware UTC."""
    local = datetime.combine(day, at, tzinfo=ZoneInfo(tz_name))
    return local.astimezone(timezone.utc)


def interview_day(scheduled_at: datetime, tz_name: str) -> date:
    """Calendar day of scheduledAt (naive UTC, often T12:00) as seen in `tz_name`."""
    aware = scheduled_at if scheduled_at.tzinfo else scheduled_at.replace(tzinfo=timezone.utc)
    return aware.astimezone(ZoneInfo(tz_name)).date()


def build_caller(
    body: CallerIn,
    *,
    scheduled_at: datetime,
    owner_id: PydanticObjectId,
    valid_coworker_ids: set[str],
    default_tz: Optional[str],
    previous: Optional[InterviewCaller] = None,
) -> InterviewCaller:
    """Validate the payload and return the caller subdocument to store.

    `valid_coworker_ids` are ids of existing active users; unknown ids are
    rejected and the owner is dropped from their own coworker list.
    """
    if not body.enabled:
        if previous is None:
            return InterviewCaller(enabled=False)
        return previous.model_copy(update={"enabled": False})

    tz_raw = (body.timezone or "").strip()
    if tz_raw:
        try:
            tz_name = validate_timezone(tz_raw)
        except ValueError as exc:
            raise CallerError(str(exc)) from exc
    else:
        tz_name = normalize_slack_timezone(default_tz)

    method = (body.method or "").strip() or None
    if method is not None and method not in CALLER_METHODS:
        raise CallerError("invalid caller method")

    ids: list[PydanticObjectId] = []
    seen: set[str] = set()
    for raw in body.coworkerIds or []:
        key = str(raw).strip()
        if not key or key in seen or key == str(owner_id):
            continue
        if key not in valid_coworker_ids:
            raise CallerError(f"unknown coworker: {key}")
        seen.add(key)
        ids.append(PydanticObjectId(key))

    day = parse_day(body.date) or interview_day(scheduled_at, tz_name)
    at = parse_hhmm(body.time)
    starts_at = local_start_utc(day, at, tz_name) if at else None

    return InterviewCaller(
        enabled=True,
        callerName=(body.callerName or "").strip() or "TBD",
        startsAt=starts_at,
        timezone=tz_name,
        method=method,
        methodValue=(body.methodValue or "").strip(),
        coworkerIds=ids,
        slackChannelTs=previous.slackChannelTs if previous else None,
    )


def resync_caller_day(caller: Optional[InterviewCaller], scheduled_at: Optional[datetime]) -> None:
    """Keep the caller's local start time when the interview date moves."""
    if caller is None or caller.startsAt is None or scheduled_at is None:
        return
    tz = ZoneInfo(normalize_slack_timezone(caller.timezone))
    local = caller.startsAt.astimezone(tz)
    day = interview_day(scheduled_at, tz.key)
    if local.date() != day:
        caller.startsAt = local_start_utc(day, local.time().replace(tzinfo=None), tz.key)


def caller_snapshot(caller: Optional[InterviewCaller]) -> Optional[tuple[Any, ...]]:
    """Fields whose change is worth a Slack thread reply."""
    if caller is None or not caller.enabled:
        return None
    return (
        caller.callerName,
        caller.startsAt,
        caller.timezone,
        caller.method,
        caller.methodValue,
        tuple(sorted(str(i) for i in caller.coworkerIds)),
    )


def is_coworker(caller: Optional[InterviewCaller], user_id: Any) -> bool:
    if caller is None or not caller.enabled:
        return False
    return any(str(i) == str(user_id) for i in caller.coworkerIds)
