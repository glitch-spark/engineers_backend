"""Caller list: everyone reads it; admins and interview managers add, edit and delete callers."""

from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel
from pymongo.errors import DuplicateKeyError

from app.deps import get_current_user
from app.models import Interview, UserExtra
from app.models.caller import CALLER_COLORS, DEFAULT_CALLERS, Caller
from app.routers._util import to_object_id

router = APIRouter(prefix="/callers", tags=["callers"])

MAX_NAME = 60


def is_caller_manager(user: UserExtra) -> bool:
    return user.role in ("admin", "interview_manager")


async def require_caller_manager(user: Annotated[UserExtra, Depends(get_current_user)]) -> UserExtra:
    if not is_caller_manager(user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only Admin or an interview manager can do this.")
    return user


class CallerIn(BaseModel):
    name: Optional[str] = None
    color: Optional[str] = None


def caller_out(c: Caller) -> dict:
    return {"_id": str(c.id), "name": c.name, "color": c.color}


def _clean_name(raw: Optional[str]) -> str:
    name = " ".join((raw or "").split())
    if not name or len(name) > MAX_NAME:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Name must be 1–{MAX_NAME} characters.")
    if name.lower() == "tbd":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="TBD means no caller yet; pick another name.")
    return name


def _check_color(color: str) -> str:
    if color not in CALLER_COLORS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unknown colour.")
    return color


async def _next_color() -> str:
    """The first colour no caller has yet (cycles once all are used)."""
    callers = await Caller.find_all().to_list()
    used = [c.color for c in callers]
    return min(CALLER_COLORS, key=lambda col: (used.count(col), CALLER_COLORS.index(col)))


async def seed_callers() -> int:
    """The starting list, added once when the collection is first created."""
    added = 0
    for i, name in enumerate(DEFAULT_CALLERS):
        try:
            await Caller(name=name, nameKey=name.lower(), color=CALLER_COLORS[i % len(CALLER_COLORS)]).insert()
            added += 1
        except DuplicateKeyError:
            pass
    return added


async def get_caller(caller_id: str) -> Optional[Caller]:
    try:
        return await Caller.get(to_object_id(caller_id))
    except HTTPException:
        return None


@router.get("")
async def list_callers(user: Annotated[UserExtra, Depends(get_current_user)]) -> dict:
    callers = await Caller.find_all().to_list()
    callers.sort(key=lambda c: c.nameKey)
    return {"callers": [caller_out(c) for c in callers], "canManage": is_caller_manager(user)}


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_caller(body: CallerIn, user: Annotated[UserExtra, Depends(require_caller_manager)]) -> dict:
    name = _clean_name(body.name)
    color = _check_color(body.color) if body.color else await _next_color()
    try:
        c = await Caller(name=name, nameKey=name.lower(), color=color).insert()
    except DuplicateKeyError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"{name} is already on the list.")
    return caller_out(c)


async def _rename_on_rounds(caller_id: str, name: str) -> None:
    """Rounds keep the caller's name for Slack and older screens: follow the rename."""
    coll = Interview.get_motor_collection()
    # One positional update per round: only the name field is written, so a concurrent round edit isn't undone.
    async for doc in coll.find({"stageHistory.caller.callerId": caller_id}, {"stageHistory.id": 1, "stageHistory.caller": 1}):
        for e in doc.get("stageHistory") or []:
            if (e.get("caller") or {}).get("callerId") == caller_id and e.get("id"):
                await coll.update_one(
                    {"_id": doc["_id"], "stageHistory.id": e["id"]},
                    {"$set": {"stageHistory.$.caller.callerName": name}},
                )
    await coll.update_many({"caller.callerId": caller_id}, {"$set": {"caller.callerName": name}})


@router.put("/{caller_id}")
async def update_caller(
    caller_id: str, body: CallerIn, user: Annotated[UserExtra, Depends(require_caller_manager)],
) -> dict:
    c = await get_caller(caller_id)
    if c is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Caller not found.")
    renamed = False
    if body.name is not None:
        name = _clean_name(body.name)
        renamed = name != c.name
        c.name, c.nameKey = name, name.lower()
    if body.color is not None:
        c.color = _check_color(body.color)
    try:
        await c.save()
    except DuplicateKeyError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"{c.name} is already on the list.")
    if renamed:
        await _rename_on_rounds(str(c.id), c.name)
    return caller_out(c)


@router.delete("/{caller_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_caller(caller_id: str, user: Annotated[UserExtra, Depends(require_caller_manager)]) -> Response:
    """Rounds already assigned keep the caller's name."""
    c = await get_caller(caller_id)
    if c is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Caller not found.")
    await c.delete()
    return Response(status_code=status.HTTP_204_NO_CONTENT)
