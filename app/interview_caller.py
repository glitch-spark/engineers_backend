"""Caller mode for interviews: a coworker joins the call at an exact time."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable, List, Optional

from beanie import PydanticObjectId
from pydantic import BaseModel

from app.interview_normalize import normalize_stage, normalize_status
from app.models.interview import InterviewCaller

CALLER_METHODS = ("video", "phone_hushed", "phone_slynumber")
# Statuses that call a round off: its #caller post is marked canceled.
ENDED_STATUSES = ("canceled", "rejected")


class CallerIn(BaseModel):
    # Older clients also send `timezone`; it is ignored (Slack shows each
    # reader their own time).
    enabled: bool = False
    callerName: Optional[str] = None
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
        "confirmed": bool(getattr(entry, "confirmed", False)),
    }


def _is_on(snapshot: Optional[dict]) -> bool:
    """A caller is requested and the round is not called off."""
    return snapshot is not None and snapshot.get("status") not in ENDED_STATUSES


def caller_post_kind(
    prev: Optional[dict], new: Optional[dict], *, has_post: bool, round_deleted: bool = False,
) -> Optional[str]:
    """What a write does to a round's #caller post.

    created → new main post; updated / canceled / restored → main post edited
    plus a thread reply; deleted → main post removed; None → nothing.
    `has_post` is whether the round already has a main post.
    """
    if round_deleted:
        return "deleted" if has_post else None
    was_on, is_on = _is_on(prev), _is_on(new)
    if not has_post:
        # Also retries a first post that failed, on the next real change.
        return "created" if is_on and new != prev else None
    if was_on and not is_on:
        return "canceled"
    if is_on and not was_on:
        return "restored"
    if is_on and new != prev:
        return "updated"
    return None


def caller_thread_event(
    prev: Optional[dict], new: Optional[dict], *, added: bool = False, removed: bool = False,
) -> Optional[str]:
    """The thread reply a write earns for one round (first match wins).

    moved (round added) · removed · ended (canceled/rejected) · back_on · rescheduled · confirmed ·
    unconfirmed · details (any other caller field) · None. Turning the caller on or off earns no reply:
    the main post says so.
    """
    if removed:
        return "removed"
    if added:
        return "moved"
    if prev is None or new is None:
        return None
    was_on, is_on = _is_on(prev), _is_on(new)
    if was_on and not is_on:
        return "ended"
    if is_on and not was_on:
        return "back_on"
    if prev.get("time") != new.get("time"):
        return "rescheduled"
    if prev.get("confirmed") != new.get("confirmed"):
        return "confirmed" if new.get("confirmed") else "unconfirmed"
    if prev != new:
        return "details"
    return None


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
