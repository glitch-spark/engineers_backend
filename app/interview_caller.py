"""Caller mode for interviews: a caller joins the call at an exact time."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from pydantic import BaseModel

from app.interview_normalize import normalize_stage, normalize_status
from app.models.interview import InterviewCaller

CALLER_KINDS = ("external", "user")
CALLER_METHODS = ("video", "phone_hushed", "phone_slynumber")
# Statuses that call a round off: its #caller post is marked canceled.
ENDED_STATUSES = ("canceled", "rejected")


def caller_unassigned(caller: Any) -> bool:
    """A caller round nobody is on yet: no caller id and no typed name (or the "TBD" placeholder). A name typed
    before the caller list existed counts as a named, external caller."""
    if caller is None or getattr(caller, "callerId", None):
        return False
    return (getattr(caller, "callerName", "") or "").strip().upper() in ("", "TBD")


class CallerIn(BaseModel):
    # Older clients also send `timezone`; it is ignored (Slack shows each
    # reader their own time).
    enabled: bool = False
    callerName: Optional[str] = None
    # From the caller list; "" = not assigned. The router resolves it and sets callerName. When both are left
    # out, the round keeps the caller it has.
    callerId: Optional[str] = None
    # "external" | "user". The router sets it from the directory; a client's value is not trusted.
    callerKind: Optional[str] = None
    method: Optional[str] = None
    methodValue: Optional[str] = None


class CallerError(ValueError):
    pass


def _as_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def build_round_caller(
    body: CallerIn,
    *,
    starts_at: datetime,
    previous: Optional[InterviewCaller] = None,
) -> InterviewCaller:
    """Caller subdocument for a round. The caller joins at the round's start,
    so there is no separate caller time."""
    if not body.enabled:
        if previous is None:
            return InterviewCaller(enabled=False)
        return previous.model_copy(update={"enabled": False})

    method = (body.method or "").strip() or None
    if method is not None and method not in CALLER_METHODS:
        raise CallerError("invalid caller method")

    if body.callerId is None and body.callerName is None and previous is not None:
        caller_id, name, kind = previous.callerId, previous.callerName, previous.callerKind
    else:
        caller_id, name = (body.callerId or None), (body.callerName or "").strip() or "TBD"
        kind = body.callerKind
        if kind is None and caller_id is not None and previous is not None and caller_id == previous.callerId:
            kind = previous.callerKind  # same caller sent back without a kind
        if kind not in CALLER_KINDS or caller_id is None:
            kind = "external"
    return InterviewCaller(
        enabled=True,
        callerName=name,
        callerId=caller_id,
        callerKind=kind,
        startsAt=_as_utc(starts_at),
        method=method,
        methodValue=(body.methodValue or "").strip(),
        slackChannelTs=previous.slackChannelTs if previous else None,
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
        "confirmed": bool(getattr(entry, "confirmed", False)),
    }


def round_post_state(entry: Any) -> dict:
    """What the interview's post shows about one round: the caller snapshot, or just the round's status when no
    caller is requested (so a status change on such a round still re-renders an existing post)."""
    return round_caller_snapshot(entry) or {"status": normalize_status(getattr(entry, "status", None))}


def caller_is_on(snapshot: Optional[dict]) -> bool:
    """A caller is requested and the round is not called off."""
    return snapshot is not None and snapshot.get("status") not in ENDED_STATUSES

