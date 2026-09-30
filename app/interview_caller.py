"""Caller mode for interviews: a coworker joins the call at an exact time."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, List, Optional

from beanie import PydanticObjectId
from pydantic import BaseModel

from app.interview_normalize import normalize_stage, normalize_status
from app.models.interview import InterviewCaller
from app.slack_timezones import normalize_slack_timezone, validate_timezone

CALLER_METHODS = ("video", "phone_hushed", "phone_slynumber")


class CallerIn(BaseModel):
    enabled: bool = False
    callerName: Optional[str] = None
    # Display time zone for Slack; the caller joins at the round's start.
    timezone: Optional[str] = None
    method: Optional[str] = None
    methodValue: Optional[str] = None
    coworkerIds: Optional[List[str]] = None


class CallerError(ValueError):
    pass


def is_coworker(caller: Optional[InterviewCaller], user_id: Any) -> bool:
    if caller is None or not caller.enabled:
        return False
    return any(str(i) == str(user_id) for i in caller.coworkerIds)


def _as_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def build_round_caller(
    body: CallerIn,
    *,
    starts_at: datetime,
    owner_id: PydanticObjectId,
    valid_coworker_ids: set[str],
    default_tz: Optional[str],
    previous: Optional[InterviewCaller] = None,
) -> InterviewCaller:
    """Caller subdocument for a round. The caller joins at the round's start,
    so there is no separate caller time; `timezone` only drives Slack display."""
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

    return InterviewCaller(
        enabled=True,
        callerName=(body.callerName or "").strip() or "TBD",
        startsAt=_as_utc(starts_at),
        timezone=tz_name,
        method=method,
        methodValue=(body.methodValue or "").strip(),
        coworkerIds=ids,
        slackChannelTs=previous.slackChannelTs if previous else None,
    )


# Order of lines in a thread reply; keys of round_caller_snapshot.
_CHANGE_FIELDS = (
    ("time", "Time"),
    ("stage", "Stage"),
    ("status", "Status"),
    ("method", "Method"),
    ("link", "Link"),
    ("caller", "Caller assigned"),
    ("coworkers", "Coworkers"),
)


def round_caller_snapshot(entry: Any) -> Optional[dict]:
    """What #caller cares about for one round; None when no caller is requested."""
    caller = getattr(entry, "caller", None)
    if caller is None or not caller.enabled:
        return None
    return {
        "time": _as_utc(getattr(entry, "scheduledAt", None)),
        "stage": normalize_stage(getattr(entry, "stage", None)),
        "status": normalize_status(getattr(entry, "status", None)),
        "method": caller.method,
        "link": (caller.methodValue or "").strip(),
        "caller": (caller.callerName or "").strip() or "TBD",
        "coworkers": tuple(sorted(str(i) for i in caller.coworkerIds)),
    }


def caller_post_kind(
    prev: Optional[dict], new: Optional[dict], *, round_deleted: bool = False,
) -> Optional[str]:
    """created / updated / canceled / None for a round's #caller thread."""
    if prev is None:
        if new is None or round_deleted or new.get("status") == "canceled":
            return None
        return "created"
    if round_deleted or new is None:
        return "canceled"
    if new.get("status") == "canceled" and prev.get("status") != "canceled":
        return "canceled"
    return "updated" if new != prev else None


def caller_change_lines(
    prev: dict,
    new: dict,
    *,
    fmt_time: Callable[[datetime], str],
    label: Callable[[str, Any], str],
) -> list[str]:
    """`Field: old → new` for each field that changed, in a fixed order."""
    lines = []
    for key, title in _CHANGE_FIELDS:
        old, cur = prev.get(key), new.get(key)
        if old == cur:
            continue
        if key == "time":
            render = lambda v: fmt_time(v) if v is not None else "TBD"  # noqa: E731
        else:
            render = lambda v, k=key: label(k, v) if v not in (None, "", ()) else "—"  # noqa: E731
        lines.append(f"{title}: {render(old)} → {render(cur)}")
    return lines
