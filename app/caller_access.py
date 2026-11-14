"""Who may act on a caller round."""

from __future__ import annotations

from typing import Optional

from beanie import PydanticObjectId

from app.models import Interview, UserExtra
from app.models.interview import InterviewStageEvent

MANAGER_ROLES = ("admin", "interview_manager")


def _is_user_caller(entry: InterviewStageEvent, user: UserExtra) -> bool:
    caller = entry.caller
    return bool(
        caller is not None
        and caller.enabled
        and caller.callerKind == "user"
        and caller.callerId
        and caller.callerId == str(user.id)
    )


def is_assigned_caller(iv: Interview, user: UserExtra) -> bool:
    """The user is the platform-user caller of at least one round of this interview. A deactivated user is not
    (get_current_user only checks the account exists, so a still-valid token must not open anything here)."""
    if getattr(user, "isActive", True) is False:
        return False
    return any(_is_user_caller(e, user) for e in iv.stageHistory)


def can_confirm(iv: Interview, entry: InterviewStageEvent, user: UserExtra) -> bool:
    """Admin, interview manager, the interview's owner, or this round's assigned user caller."""
    return (
        user.role in MANAGER_ROLES
        or str(iv.createdBy) == str(user.id)
        or _is_user_caller(entry, user)
    )


async def caller_interview(interview_id: Optional[str], user: UserExtra) -> Optional[Interview]:
    """The interview named by an `interviewId` query value when `user` is its assigned caller, else None. A missing,
    malformed or unknown id is None as well, so callers fall back to their normal checks."""
    if not interview_id:
        return None
    try:
        oid = PydanticObjectId(interview_id)
    except Exception:
        return None
    iv = await Interview.get(oid)
    return iv if iv is not None and is_assigned_caller(iv, user) else None
