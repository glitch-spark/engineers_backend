"""Routes the Bid Track extension calls: register with an invite code, log in, who am I."""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal, Optional

from beanie.exceptions import RevisionIdWasChanged
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import AwareDatetime, BaseModel, Field
from pymongo.errors import DuplicateKeyError

from app.bids import rate_limit
from app.bids.folders import folder_url
from app.bids.invites import hash_code, invite_is_live
from app.bids.rate_limit import FailureLimiter, client_ip
from app.bids.schemas import LoginIn, RegisterIn, bidder_me
from app.bids.storage import BidStorageError, get_bid_storage
from app.bids.upload_keys import build_upload_key
from app.config import get_settings
from app.deps import get_current_bidder
from app.models import Account
from app.models.bidder import Bidder
from app.security import create_bidder_token, hash_password, verify_password

router = APIRouter(prefix="/ext", tags=["bidder-ext"])

LIMITER: FailureLimiter = rate_limit.LIMITER

_BAD_LOGIN = "Wrong username or password"
_TAKEN = "Username is taken"
_BAD_CODE = "Invite code is invalid or expired"
# Verified against when the username is unknown or unusable, so every login failure costs one bcrypt check.
_DUMMY_HASH = hash_password("not-a-real-password")


def _keys(username: str, request: Request) -> list[str]:
    return [f"user:{username}", f"ip:{client_ip(request)}"]


def _reserve(keys: list[str]) -> datetime:
    """Count this attempt as a failure before any await (concurrent requests can't all slip past the check);
    a success gives it back. 429 when a key is already at the limit."""
    now = datetime.now(timezone.utc)
    wait = LIMITER.reserve(keys, now)
    if wait is not None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many attempts — try again later",
            headers={"Retry-After": str(wait)},
        )
    return now


def _succeed(keys: list[str], reserved_at: datetime) -> None:
    """Successes never count toward the IP limit (many bidders share an office IP); clear the username."""
    user_key, ip_key = keys
    LIMITER.release([ip_key], reserved_at)
    LIMITER.clear(user_key)


async def _profile_name(bidder: Bidder) -> Optional[str]:
    if bidder.profileId is None:
        return None
    account = await Account.get(bidder.profileId)
    return account.name if account else None


async def _session(bidder: Bidder) -> dict:
    return {
        "token": create_bidder_token(str(bidder.id), bidder.tokenVersion),
        "bidder": bidder_me(bidder, await _profile_name(bidder)),
    }


async def register_bidder_row(bidder: Bidder, username: str, password_hash: str) -> bool:
    """Claim the invite in ONE conditional write. False when the code was reused, replaced, or the bidder
    was archived or registered since it was read (the filter, not our earlier read, is the guard)."""
    now = datetime.now(timezone.utc)
    updates = {"username": username, "passwordHash": password_hash, "registeredAt": now, "updatedAt": now}
    bucket = get_settings().BID_STORAGE_BUCKET
    if bucket and bidder.folder:  # counting switches to the folder the extension uploads to
        updates["screenshotFolderUrl"] = folder_url(bucket, bidder.folder)
    result = await Bidder.get_motor_collection().update_one(
        {
            "_id": bidder.id,
            "inviteCodeHash": bidder.inviteCodeHash,
            "registeredAt": {"$exists": False},
            "archivedAt": {"$exists": False},
        },
        {
            "$set": updates,
            "$unset": {"inviteCodeHash": "", "inviteExpiresAt": ""},
        },
    )
    return result.matched_count == 1


@router.post("/register")
async def register(body: RegisterIn, request: Request) -> dict:
    keys = _keys(body.username, request)
    reserved_at = _reserve(keys)
    bidder = await Bidder.find_one(Bidder.inviteCodeHash == hash_code(body.inviteCode))
    if bidder is None or not invite_is_live(bidder, datetime.now(timezone.utc)):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, _BAD_CODE)
    if await Bidder.find_one(Bidder.username == body.username) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, _TAKEN)
    try:
        claimed = await register_bidder_row(bidder, body.username, hash_password(body.password))
    except (DuplicateKeyError, RevisionIdWasChanged):  # a concurrent registration took the username
        raise HTTPException(status.HTTP_409_CONFLICT, _TAKEN)
    if not claimed:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, _BAD_CODE)
    _succeed(keys, reserved_at)
    return await _session(await Bidder.get(bidder.id))


@router.post("/login")
async def login(body: LoginIn, request: Request) -> dict:
    keys = _keys(body.username, request)
    reserved_at = _reserve(keys)
    bidder = await Bidder.find_one(Bidder.username == body.username) if body.username else None
    usable = bidder is not None and bidder.archivedAt is None and bidder.registeredAt is not None
    stored = bidder.passwordHash if usable and bidder.passwordHash else None
    try:
        password_ok = verify_password(body.password, stored or _DUMMY_HASH)
    except ValueError:  # e.g. a password bcrypt refuses to hash: just a wrong password
        password_ok = False
    if not (usable and stored and password_ok):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, _BAD_LOGIN)
    _succeed(keys, reserved_at)
    return await _session(bidder)


@router.get("/me")
async def me(bidder: Annotated[Bidder, Depends(get_current_bidder)]) -> dict:
    return bidder_me(bidder, await _profile_name(bidder))


class UploadUrlIn(BaseModel):
    job: str = Field(min_length=1, max_length=200)
    step: int = Field(ge=1, le=500)
    trigger: Literal["step", "next", "submit"]
    capturedAt: AwareDatetime


@router.post("/upload-url")
async def upload_url(body: UploadUrlIn, bidder: Annotated[Bidder, Depends(get_current_bidder)]) -> dict:
    """The backend picks the object path; the extension only PUTs the PNG to the returned link."""
    unavailable = HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Uploads are not configured")
    bucket = get_settings().BID_STORAGE_BUCKET
    storage = get_bid_storage()
    if not bucket or not hasattr(storage, "presign_put"):
        raise unavailable
    try:
        key = build_upload_key(
            bidder.folder, await _profile_name(bidder), body.job, body.step, body.trigger,
            body.capturedAt, datetime.now(timezone.utc),
        )
    except ValueError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "capturedAt is not current")
    try:
        url = await asyncio.to_thread(storage.presign_put, bucket, key)
    except BidStorageError:
        raise unavailable from None
    return {
        "url": url,
        "key": key,
        "expiresAt": (datetime.now(timezone.utc) + timedelta(seconds=300)).isoformat(),
        "headers": {"Content-Type": "image/png"},
    }
