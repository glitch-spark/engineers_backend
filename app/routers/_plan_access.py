"""Read/write rules and input parsing shared by the plan routers.

Only the owner writes a plan (admins get 403). The owner or an admin can
read it; staff reading someone else get 403.
"""

import re
from datetime import date
from typing import Optional, TypeVar

from beanie import PydanticObjectId
from fastapi import HTTPException, status
from pydantic import BaseModel, ValidationError

from app.models import UserExtra
from app.routers._util import to_object_id

_YMD_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

M = TypeVar("M", bound=BaseModel)


async def read_subject(user: UserExtra, user_id: Optional[str]) -> PydanticObjectId:
    """Whose plans to read: the caller, or (admins only) `user_id`."""
    if not user_id or user_id == str(user.id):
        return user.id
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You can only view your own plans")
    sid = to_object_id(user_id)
    if await UserExtra.get(sid) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="user not found")
    return sid


def require_writer(user: UserExtra) -> None:
    if user.role == "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admins can view plans but cannot change them",
        )


def parse_ymd(value: str, name: str) -> date:
    """Strict YYYY-MM-DD with a year in 2000..2100, else 422."""
    raw = (value or "").strip()
    try:
        if not _YMD_RE.match(raw):
            raise ValueError
        parsed = date.fromisoformat(raw)
        if not 2000 <= parsed.year <= 2100:
            raise ValueError
    except ValueError:
        raise HTTPException(status_code=422, detail=f"invalid {name}")
    return parsed


def parse_monday(value: str, name: str = "weekStart") -> date:
    day = parse_ymd(value, name)
    if day.weekday() != 0:
        raise HTTPException(status_code=422, detail=f"{name} must be a Monday")
    return day


def parse_body(model: type[M], payload: dict) -> M:
    """Validate a JSON body as `model`, answering 422 with the first problem.

    The app rewrites FastAPI's own validation errors to 400, so plan bodies
    are validated here to keep the documented 422.
    """
    try:
        return model.model_validate(payload or {})
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first.get("loc", ()))
        raise HTTPException(status_code=422, detail=f"{where}: {first.get('msg')}" if where else first.get("msg"))
