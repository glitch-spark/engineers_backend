"""Routes the Bid Track extension calls for owners (spec 2026-11-15-self-bids-extension-design.md): an owner signs in
with their platform login and one of their active profiles; that sign-in records every apply in that Chrome profile
for that profile (self bids). Tokens are kept apart from app and bidder tokens (typ="ext-owner")."""
from datetime import datetime, timezone
from typing import Annotated

from beanie import PydanticObjectId
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

import asyncio
from datetime import timedelta

from app.bids.records import confirm_self_upload, record_self_screenshot
from app.bids.storage import BidStorageError, get_bid_storage
from app.bids.upload_keys import IMAGE_FORMATS, build_upload_key
from app.config import get_settings
from app.deps import ExtOwnerAuth, get_current_ext_owner
from app import self_bids
from app.bids.windows import schedule_from_settings
from app.models.bid import Attachment
from app.profile_metrics import bid_report_day
from app.models import Account, UserExtra
from app.routers.auth import _ci_eq, _password_ok
from app.routers.bidder_ext import _JOB_TITLE_MAX, ConfirmIn, UploadUrlIn, _job_url, _keys, _reserve, _succeed
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


@router.post("/upload-url")
async def owner_upload_url(body: UploadUrlIn, auth: Annotated[ExtOwnerAuth, Depends(get_current_ext_owner)]) -> dict:
    """As the bidder route: the server picks the object path (folder "self-<ownerId>", the signed-in profile) and
    records the screenshot on the owner's self bid for that job."""
    job_url = _job_url(body.jobUrl)
    unavailable = HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Uploads are not configured")
    bucket = get_settings().BID_STORAGE_BUCKET
    storage = get_bid_storage()
    if not bucket or not hasattr(storage, "presign_put"):
        raise unavailable
    try:
        key = build_upload_key(f"self-{auth.user.id}", auth.account.name, body.job, body.step, body.trigger,
                               body.capturedAt, datetime.now(timezone.utc), body.format)
    except ValueError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "capturedAt is not current")
    bid = await record_self_screenshot(
        auth.user.id, auth.account.id, key, body.step, body.trigger, body.capturedAt, job_url,
        (body.jobTitle or "")[:_JOB_TITLE_MAX] or None, [Attachment(**a.model_dump()) for a in body.attachments],
    )
    # After the switch the day's Daily Plan carries the tracked count (a no-op during the pilot).
    await self_bids.write_through(auth.user.id, bid_report_day(bid.firstAt, schedule_from_settings()))
    try:
        content_type = IMAGE_FORMATS[body.format][1]
        url = await asyncio.to_thread(storage.presign_put, bucket, key, content_type)
    except BidStorageError:
        raise unavailable from None
    return {
        "url": url, "key": key, "bidId": str(bid.id),
        "expiresAt": (datetime.now(timezone.utc) + timedelta(seconds=300)).isoformat(),
        "headers": {"Content-Type": content_type}, **_renewal(auth),
    }


@router.post("/uploads/confirm")
async def owner_confirm(body: ConfirmIn, auth: Annotated[ExtOwnerAuth, Depends(get_current_ext_owner)]) -> dict:
    if not await confirm_self_upload(auth.user.id, body.key):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "upload not found")
    return {"ok": True, **_renewal(auth)}
