"""A day's rounds from the database: one `DayRound` per active round, whoever is on the call."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, Optional

from beanie import PydanticObjectId

from app.interview_caller import ENDED_STATUSES, caller_unassigned
from app.interview_normalize import interview_status_value, normalize_stage
from app.models import Account, Interview, UserExtra
from app.models.interview import InterviewStage, InterviewStageEvent
from app.slack_bot import _aware_utc, _profile_label

CallerState = Literal["self", "tbd", "external", "user"]


@dataclass
class DayRound:
    interview_id: str
    round_id: str
    start: Optional[datetime]  # aware UTC; None for a legacy round with no time yet (shown as TBD)
    company: str
    stage: str  # the stage key ("tech_round_1"); the builder labels it
    profile: str
    position: str
    round_no: int  # 1-based place of the round in the interview's stage history
    method: str  # caller method key ("video", ...), "" when none
    link: str  # the method's link or number, "" when none
    caller: CallerState  # self = no caller wanted, tbd = wanted but not assigned
    caller_name: str
    caller_user: Optional[UserExtra]  # the platform user of a "user" caller (active or not)
    confirmed: bool
    owner: Optional[UserExtra]
    thread_link: Optional[str] = None  # filled by the day post from #caller, not here
    status: str = "scheduled"  # scheduled | completed | passed | rejected | canceled
    caller_id: str = ""  # the user's or external caller's id; "" for self / tbd


def _is_round(entry: Any, start: datetime, end: datetime) -> bool:
    at = getattr(entry, "scheduledAt", None)
    return bool(
        at is not None
        and start <= _aware_utc(at) < end
        and normalize_stage(entry.stage) != InterviewStage.home_assessment.value
        and interview_status_value(getattr(entry, "status", None)) not in ENDED_STATUSES
    )


def _object_id(raw: Any) -> Optional[PydanticObjectId]:
    try:
        return PydanticObjectId(str(raw))
    except Exception:
        return None


def _caller_state(caller: Any) -> CallerState:
    if caller is None or not caller.enabled:
        return "self"
    if caller_unassigned(caller):
        return "tbd"
    return "user" if caller.callerKind == "user" else "external"


async def _by_id(model: Any, ids: set[PydanticObjectId]) -> dict[str, Any]:
    if not ids:
        return {}
    return {str(doc.id): doc for doc in await model.find({"_id": {"$in": list(ids)}}).to_list()}


def day_round(
    iv: Interview, no: int, entry: InterviewStageEvent, *,
    owner: Optional[UserExtra], account: Optional[Account], caller_user: Optional[UserExtra],
) -> DayRound:
    """One round as the Slack builders read it. `caller_user` is the platform user of a "user" caller."""
    caller, state = entry.caller, _caller_state(entry.caller)
    user = caller_user if state == "user" else None
    name = "Self" if state == "self" else ((user.name or user.email) if user else None) or caller.callerName
    at = entry.scheduledAt
    return DayRound(
        interview_id=str(iv.id),
        round_id=entry.id,
        start=_aware_utc(at) if at is not None else None,
        company=(iv.companyName or "").strip(),
        stage=normalize_stage(entry.stage) or "",
        profile=_profile_label(account),
        position=(iv.appliedPosition or "").strip(),
        round_no=no,
        # A self round is not a caller call: a method kept from an earlier caller is not shown.
        method=(caller.method or "") if state != "self" else "",
        link=(caller.methodValue or "").strip() if state != "self" else "",
        caller=state,
        caller_name=name,
        caller_user=user,
        confirmed=bool(entry.confirmed),
        owner=owner,
        status=interview_status_value(entry.status) or "scheduled",
        caller_id=(caller.callerId or "") if state in ("user", "external") else "",
    )


async def load_rounds(start: datetime, end: datetime) -> list[DayRound]:
    """Active rounds starting in [start, end) of every interview, by start. Home assessments are not calls and
    ended (canceled / rejected) rounds are off, so neither is listed."""
    start, end = _aware_utc(start), _aware_utc(end)
    found = await Interview.find({"stageHistory": {"$elemMatch": {
        "status": {"$nin": list(ENDED_STATUSES)},
        "scheduledAt": {"$gte": start.replace(tzinfo=None), "$lt": end.replace(tzinfo=None)},
    }}}).to_list()
    rows = [(iv, no, e) for iv in found for no, e in enumerate(iv.stageHistory, 1) if _is_round(e, start, end)]
    if not rows:
        return []

    owners = await _by_id(UserExtra, {iv.createdBy for iv, _, _ in rows})
    accounts = await _by_id(Account, {iv.accountId for iv, _, _ in rows if iv.accountId})
    caller_ids = {_object_id(e.caller.callerId) for _, _, e in rows if _caller_state(e.caller) == "user"}
    callers = await _by_id(UserExtra, {i for i in caller_ids if i is not None})

    out = [
        day_round(iv, no, e, owner=owners.get(str(iv.createdBy)), account=accounts.get(str(iv.accountId)),
                  caller_user=callers.get(str(e.caller.callerId)) if _caller_state(e.caller) == "user" else None)
        for iv, no, e in rows
    ]
    out.sort(key=lambda r: r.start)  # every listed round has a time
    return out
