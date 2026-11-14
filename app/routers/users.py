from datetime import datetime
from typing import Annotated, Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from app.caller_directory import user_display_name
from app.deps import get_current_user, require_admin
from app.models import UserExtra
from app.models.user_extra import UserRole
from app.routers._util import ci_regex, doc_to_dict, to_object_id
from app.routers.callers import rename_on_rounds
from app.slack_bot import normalize_slack_id

router = APIRouter(prefix="/users", tags=["users"])


class UserCreate(BaseModel):
    name: str
    email: str
    role: UserRole = "staff"
    phone: Optional[str] = ""
    birthday: Optional[str] = None


class UserUpdate(BaseModel):
    name: Optional[str] = None
    role: Optional[UserRole] = None
    phone: Optional[str] = None
    birthday: Optional[str] = None
    isActive: Optional[bool] = None
    # The user's Slack member ID (U…); blank clears it. Left out = unchanged.
    slackUserId: Optional[str] = None


def _parse_dt(s: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


@router.get("/lookup")
async def lookup_users(
    _: Annotated[UserExtra, Depends(get_current_user)],
    excludeRole: Optional[str] = Query(None),
) -> dict:
    """Minimal projection of every user, for filter dropdowns. Auth required, no admin gate."""
    query: dict[str, Any] = {}
    if excludeRole:
        query["role"] = {"$ne": excludeRole}
    rows = await UserExtra.find(query).to_list()
    return {
        "users": [
            {"_id": str(u.id), "name": u.name, "email": u.email, "role": u.role, "isActive": u.isActive is not False}
            for u in rows
        ]
    }


@router.get("")
async def list_users(
    _: Annotated[UserExtra, Depends(require_admin)],
    search: Optional[str] = Query(None),
    role: Optional[str] = Query(None),
    status_: Optional[str] = Query(None, alias="status"),
    page: int = Query(1),
    limit: int = Query(10),
) -> dict:
    query: dict[str, Any] = {}

    if search:
        query["$or"] = [
            {"name": ci_regex(search)},
            {"email": ci_regex(search)},
            {"phone": ci_regex(search)},
        ]
    if role:
        query["role"] = role
    if status_ == "inactive":
        query["isActive"] = False
    elif status_ == "active":
        query["isActive"] = {"$ne": False}

    skip = (page - 1) * limit
    total = await UserExtra.find(query).count()
    docs = (
        await UserExtra.find(query)
        .sort([("createdAt", -1)])
        .skip(skip)
        .limit(limit)
        .to_list()
    )

    users_out = [doc_to_dict(d) for d in docs]
    total_pages = (total + limit - 1) // limit if limit else 0
    return {
        "users": users_out,
        "pagination": {
            "total": total,
            "page": page,
            "limit": limit,
            "totalPages": total_pages,
        },
    }


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_user(
    body: UserCreate,
    _: Annotated[UserExtra, Depends(require_admin)],
) -> dict:
    if not body.email or not body.name:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Email and name are required")

    existing = await UserExtra.find_one(UserExtra.email == body.email)
    if existing:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="User with this email already exists")

    birthday = _parse_dt(body.birthday) if body.birthday else None

    user = UserExtra(
        name=body.name,
        email=body.email,
        role=body.role,
        phone=body.phone or "",
        birthday=birthday,
        hashedPassword="placeholder",
    )
    await user.insert()
    return doc_to_dict(user)


@router.put("/{user_id}")
async def update_user(
    user_id: str,
    body: UserUpdate,
    _: Annotated[UserExtra, Depends(require_admin)],
) -> dict:
    oid = to_object_id(user_id)
    user = await UserExtra.get(oid)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    old_caller_name = user_display_name(user)
    if body.name is not None:
        user.name = body.name
    if body.role is not None:
        user.role = body.role
    if body.phone is not None:
        user.phone = body.phone
    if body.birthday is not None:
        parsed = _parse_dt(body.birthday)
        if parsed is not None:
            user.birthday = parsed
    if body.isActive is not None:
        user.isActive = body.isActive
    if "slackUserId" in body.model_fields_set:
        try:
            slack_id = normalize_slack_id(body.slackUserId)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        # One Slack account → one Engineers user (same rule as Profile → Slack).
        if slack_id and await UserExtra.find_one({"slackUserId": slack_id, "_id": {"$ne": user.id}}):
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="That Slack ID is already used by another user")
        user.slackUserId = slack_id

    await user.save()
    if user_display_name(user) != old_caller_name:
        await rename_on_rounds(str(user.id), user_display_name(user))
    return doc_to_dict(user)


@router.post("/{user_id}/approve")
async def approve_user(
    user_id: str,
    _: Annotated[UserExtra, Depends(require_admin)],
) -> dict:
    oid = to_object_id(user_id)
    user = await UserExtra.get(oid)
    if user is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    user.isActive = True
    await user.save()
    return doc_to_dict(user)


@router.delete("/{user_id}")
async def delete_user(
    user_id: str,
    _: Annotated[UserExtra, Depends(require_admin)],
) -> dict:
    oid = to_object_id(user_id)
    user = await UserExtra.get(oid)
    if user is not None:
        await user.delete()
    return {"ok": True}
