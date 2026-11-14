"""Who may act on a caller round."""

from __future__ import annotations

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
    """The user is the platform-user caller of at least one round of this interview."""
    return any(_is_user_caller(e, user) for e in iv.stageHistory)


def can_confirm(iv: Interview, entry: InterviewStageEvent, user: UserExtra) -> bool:
    """Admin, interview manager, the interview's owner, or this round's assigned user caller."""
    return (
        user.role in MANAGER_ROLES
        or str(iv.createdBy) == str(user.id)
        or _is_user_caller(entry, user)
    )
