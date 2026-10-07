from datetime import datetime
from typing import Annotated, Any, Optional

from beanie import PydanticObjectId
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from app.deps import get_current_user, require_admin
from app.models import UserExtra
from app.routers._util import ci_regex, doc_to_dict, to_object_id

router = APIRouter(prefix="/users", tags=["users"])


class UserCreate(BaseModel):
    name: str
    email: str
    role: Optional[str] = "staff"
    phone: Optional[str] = ""
    birthday: Optional[str] = None


class UserUpdate(BaseModel):
    name: Optional[str] = None
    role: Optional[str] = None
    phone: Optional[str] = None
    birthday: Optional[str] = None
    isActive: Optional[bool] = None
    # Another active user; null clears it.
    interviewManagerId: Optional[str] = None


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


async def _manager_for(user: UserExtra, raw: Optional[str]) -> Optional[PydanticObjectId]:
    """The chosen interview manager: an existing active user other than `user`; None clears it."""
    if raw is None or not str(raw).strip():
        return None
    if str(raw) == str(user.interviewManagerId):
        return user.interviewManagerId  # unchanged: a since-deactivated manager doesn't block other edits
    bad = HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Choose another active user")
    if not PydanticObjectId.is_valid(raw) or str(raw) == str(user.id):
        raise bad
    manager = await UserExtra.get(PydanticObjectId(raw))
    if manager is None or manager.isActive is False:
        raise bad
    return manager.id


async def _with_manager_names(docs: list[UserExtra]) -> list[dict]:
    """User rows plus interviewManagerName (one lookup for the whole page)."""
    ids = list({d.interviewManagerId for d in docs if d.interviewManagerId})
    managers = {m.id: m for m in await UserExtra.find({"_id": {"$in": ids}}).to_list()} if ids else {}
    out = []
    for d in docs:
        row = doc_to_dict(d)
        manager = managers.get(d.interviewManagerId)
        row["interviewManagerName"] = (manager.name or manager.email) if manager else None
        out.append(row)
    return out


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

    users_out = await _with_manager_names(docs)
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
        role=body.role or "staff",
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
    if "interviewManagerId" in body.model_fields_set:
        user.interviewManagerId = await _manager_for(user, body.interviewManagerId)

    await user.save()
    return (await _with_manager_names([user]))[0]


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
