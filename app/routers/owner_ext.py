"""Routes the Bid Track extension calls for owners (spec 2026-11-15-self-bids-extension-design.md): an owner signs in
with their platform login and one of their active profiles; that sign-in records every apply in that Chrome profile
for that profile (self bids). Tokens are kept apart from app and bidder tokens (typ="ext-owner")."""
from datetime import datetime, timezone
from typing import Annotated

from beanie import PydanticObjectId
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.deps import ExtOwnerAuth, get_current_ext_owner
from app.models import Account, UserExtra
from app.routers.auth import _ci_eq, _password_ok
from app.routers.bidder_ext import _keys, _reserve, _succeed
from app.security import bidder_token_needs_renewal, create_ext_owner_token

router = APIRouter(prefix="/ext/owner", tags=["owner-ext"])

_BAD_LOGIN = "Wrong username or password"


class OwnerCredentials(BaseModel):
    username: str = Field(max_length=200)
    password: str = Field(max_length=1024)


class OwnerLoginIn(OwnerCredentials):
    profileId: str = Field(max_length=64)


async def _owner(body: OwnerCredentials, request: Request) -> UserExtra:
    """The signed-in owner: platform credentials (name or email), active, not an admin (admins own no profiles to
    apply with). Rate limited like the extension's bidder login."""
    username = body.username.strip().lower()
    keys = _keys(username, request)
    reserved_at = _reserve(keys)
    user = await UserExtra.find_one({"$or": [_ci_eq("name", username), _ci_eq("email", username)]}) if username else None
    if not await _password_ok(body.password, user.hashedPassword if user else None):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, _BAD_LOGIN)
    _succeed(keys, reserved_at)
    if not user.isActive or user.role == "admin":
        raise HTTPException(status.HTTP_403_FORBIDDEN, "This account can't record applies with the extension")
    return user


async def _active_profiles(owner_id) -> list[Account]:
    return await Account.find({"createdBy": owner_id, "archived": {"$ne": True}}).sort("name").to_list()


def _me(user: UserExtra, account: Account) -> dict:
    return {"id": str(user.id), "name": user.name or user.email or "", "profile": {"id": str(account.id), "name": account.name}}


@router.post("/profiles")
async def owner_profiles(body: OwnerCredentials, request: Request) -> dict:
    """The owner's active profiles, to choose the one this Chrome profile records for."""
    user = await _owner(body, request)
    return {"profiles": [{"id": str(a.id), "name": a.name} for a in await _active_profiles(user.id)]}


@router.post("/login")
async def owner_login(body: OwnerLoginIn, request: Request) -> dict:
    """Sign in for one of the owner's active profiles: the token records applies for that profile only."""
    user = await _owner(body, request)
    account = await Account.get(PydanticObjectId(body.profileId)) if PydanticObjectId.is_valid(body.profileId) else None
    if account is None or account.createdBy != user.id or account.archived:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Pick one of your active profiles")
    return {"token": create_ext_owner_token(str(user.id), str(account.id)), "owner": _me(user, account)}


def _renewal(auth: ExtOwnerAuth) -> dict:
    """A fresh token once the presented one is a week old, as for bidders (kept signed in while in use)."""
    if not bidder_token_needs_renewal(auth.claims, datetime.now(timezone.utc)):
        return {}
    return {"token": create_ext_owner_token(str(auth.user.id), str(auth.account.id))}


@router.get("/me")
async def owner_me(auth: Annotated[ExtOwnerAuth, Depends(get_current_ext_owner)]) -> dict:
    return {**_me(auth.user, auth.account), **_renewal(auth)}
