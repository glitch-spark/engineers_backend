"""Caller teams admin (/callers/teams): Admin and interview managers set up the teams rounds are posted for."""

from datetime import datetime, timezone
from typing import Annotated, Optional
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from pymongo.errors import DuplicateKeyError

from app.caller_teams import slack
from app.deps import get_current_user
from app.models import UserExtra
from app.models.caller import Caller
from app.models.caller_team import CallerTeam
from app.routers._util import to_object_id
from app.routers.callers import MAX_NAME, require_caller_manager, unprocessable

router = APIRouter(prefix="/callers/teams", tags=["caller-teams"])

_TIME = r"^([01]\d|2[0-3]):[0-5]\d$"


class TeamIn(BaseModel):
    name: str = Field(min_length=1, max_length=MAX_NAME)
    managerCallerId: str
    channelId: str = Field(min_length=1)
    reminderChannelId: str = Field(min_length=1)
    dailyTime: str = Field(default="06:00", pattern=_TIME)
    timezone: str = "America/Chicago"


class TeamPatch(BaseModel):
    name: Optional[str] = Field(default=None, min_length=1, max_length=MAX_NAME)
    managerCallerId: Optional[str] = None
    channelId: Optional[str] = Field(default=None, min_length=1)
    reminderChannelId: Optional[str] = Field(default=None, min_length=1)
    dailyTime: Optional[str] = Field(default=None, pattern=_TIME)
    timezone: Optional[str] = None
    archived: Optional[bool] = None


def _check_timezone(tz: str) -> None:
    try:
        ZoneInfo(tz)
    except Exception:
        raise unprocessable("Unknown timezone.")


async def _check_manager(caller_id: str) -> None:
    try:
        c = await Caller.get(to_object_id(caller_id))
    except HTTPException:
        c = None
    if c is None or c.archivedAt is not None:
        raise unprocessable("Pick an external caller as manager.")


def _out(t: CallerTeam, member_ids: list[str]) -> dict:
    return {**t.model_dump(mode="json", exclude={"id", "revision_id"}), "_id": str(t.id),
            "memberIds": member_ids, "archived": t.archivedAt is not None}


async def _member_ids(team_id: str) -> list[str]:
    return [str(c.id) for c in await Caller.find({"teamId": team_id}).to_list()]


async def _get_team(team_id: str) -> CallerTeam:
    try:
        t = await CallerTeam.get(to_object_id(team_id))
    except HTTPException:
        t = None
    if t is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Team not found.")
    return t


def _name_taken() -> HTTPException:
    return HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A team with this name already exists.")


@router.get("")
async def list_teams(user: Annotated[UserExtra, Depends(get_current_user)]) -> dict:
    members: dict[str, list[str]] = {}
    for c in await Caller.find({"teamId": {"$ne": None}}).to_list():
        members.setdefault(c.teamId or "", []).append(str(c.id))
    teams = sorted(await CallerTeam.find_all().to_list(), key=lambda t: t.nameKey)
    return {"teams": [_out(t, members.get(str(t.id), [])) for t in teams]}


@router.get("/connection")
async def connection(user: Annotated[UserExtra, Depends(require_caller_manager)]) -> dict:
    if not slack.callers_configured():
        return {"connected": False, "workspace": None}
    name = await slack.workspace_name()
    return {"connected": name is not None, "workspace": name}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_team(body: TeamIn, user: Annotated[UserExtra, Depends(require_caller_manager)]) -> dict:
    name = " ".join(body.name.split())
    _check_timezone(body.timezone)
    await _check_manager(body.managerCallerId)
    try:
        t = await CallerTeam(
            name=name, nameKey=name.lower(), managerCallerId=body.managerCallerId, channelId=body.channelId.strip(),
            reminderChannelId=body.reminderChannelId.strip(), dailyTime=body.dailyTime, timezone=body.timezone,
        ).insert()
    except DuplicateKeyError:
        raise _name_taken()
    return _out(t, [])


@router.patch("/{team_id}")
async def update_team(team_id: str, body: TeamPatch, user: Annotated[UserExtra, Depends(require_caller_manager)]) -> dict:
    t = await _get_team(team_id)
    if body.name is not None:
        t.name = " ".join(body.name.split())
        t.nameKey = t.name.lower()
    if body.managerCallerId is not None:
        await _check_manager(body.managerCallerId)
        t.managerCallerId = body.managerCallerId
    if body.timezone is not None:
        _check_timezone(body.timezone)
        t.timezone = body.timezone
    for field in ("channelId", "reminderChannelId", "dailyTime"):
        if getattr(body, field) is not None:
            setattr(t, field, getattr(body, field).strip())
    if body.archived is not None:
        t.archivedAt = (t.archivedAt or datetime.now(timezone.utc)) if body.archived else None
    try:
        await t.save()
    except DuplicateKeyError:
        raise _name_taken()
    return _out(t, await _member_ids(str(t.id)))


async def _try_post(channel: str, text: str) -> dict:
    try:
        await slack.api("chat.postMessage", {"channel": channel, "text": text})
        return {"ok": True, "error": None}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@router.post("/{team_id}/test")
async def send_test(team_id: str, user: Annotated[UserExtra, Depends(require_caller_manager)]) -> dict:
    t = await _get_team(team_id)
    text = f"Test post from the interview bot for {t.name}."
    return {"channel": await _try_post(t.channelId, text), "reminderChannel": await _try_post(t.reminderChannelId, text)}
