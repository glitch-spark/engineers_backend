"""The people a round can be assigned to: external callers plus active platform users."""

import hashlib
from typing import Literal, Optional, TypedDict

from beanie import PydanticObjectId

from app.models.caller import CALLER_COLORS, Caller
from app.models.caller_team import CallerTeam
from app.models.user_extra import UserExtra

CallerEntry = TypedDict(
    "CallerEntry",
    {"_id": str, "kind": Literal["external", "user"], "name": str, "color": str, "archived": bool,
     "slackUserId": str, "teamId": str, "teamName": str},
)


def user_caller_color(user_id: str, override: Optional[str]) -> str:
    """The user's chosen colour, else one derived from their id so it never changes."""
    if override in CALLER_COLORS:
        return override
    return CALLER_COLORS[int(hashlib.sha1(user_id.encode()).hexdigest(), 16) % len(CALLER_COLORS)]


def user_display_name(user: UserExtra) -> str:
    return (user.name or "").strip() or (user.email or "").strip() or "User"


def _user_entry(user: UserExtra) -> CallerEntry:
    uid = str(user.id)
    return {
        "_id": uid,
        "kind": "user",
        "name": user_display_name(user),
        "color": user_caller_color(uid, user.callerColor),
        "archived": False,
        "slackUserId": "",
        "teamId": "",
        "teamName": "",
    }


def _external_entry(caller: Caller, team_names: Optional[dict[str, str]] = None) -> CallerEntry:
    return {
        "_id": str(caller.id),
        "kind": "external",
        "name": caller.name,
        "color": caller.color,
        "archived": caller.archivedAt is not None,
        "slackUserId": caller.slackUserId or "",
        "teamId": caller.teamId or "",
        "teamName": (team_names or {}).get(caller.teamId or "", ""),
    }


async def team_names() -> dict[str, str]:
    return {str(t.id): t.name for t in await CallerTeam.find_all().to_list() if t.archivedAt is None}


async def list_directory(*, include_archived: bool = False) -> list[CallerEntry]:
    """Active users first (by name), then external callers (by nameKey)."""
    users = await UserExtra.find({"isActive": {"$ne": False}}).to_list()
    users.sort(key=lambda u: user_display_name(u).casefold())
    externals = await Caller.find_all().to_list()
    if not include_archived:
        externals = [c for c in externals if c.archivedAt is None]
    externals.sort(key=lambda c: c.nameKey)
    names = await team_names()
    return [_user_entry(u) for u in users] + [_external_entry(c, names) for c in externals]


async def resolve_caller(caller_id: str) -> Optional[CallerEntry]:
    """An active user or a non-archived external caller; None for anything else."""
    try:
        oid = PydanticObjectId(caller_id)
    except Exception:
        return None
    user = await UserExtra.get(oid)
    if user is not None:
        return _user_entry(user) if user.isActive is not False else None
    caller = await Caller.get(oid)
    if caller is not None and caller.archivedAt is None:
        return _external_entry(caller, await team_names())
    return None
