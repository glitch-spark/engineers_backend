"""Routes the Bid Track extension calls: register with an invite code, log in, who am I."""

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Annotated, Literal, Optional
from urllib.parse import urlsplit

from beanie.exceptions import RevisionIdWasChanged
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import AwareDatetime, BaseModel, Field, StringConstraints, ValidationInfo, field_validator
from pymongo.errors import DuplicateKeyError

from app.bids import rate_limit
from app.bids.folders import folder_url
from app.bids.invites import hash_code, invite_is_live
from app.bids.rate_limit import FailureLimiter, client_ip, limiter_keys
from app.bids.records import confirm_upload, record_screenshot
from app.bids.schemas import (
    LoginIn, PasswordChangeIn, RegisterIn, bidder_me, credentials_problem, password_problem,
)
from app.bids.storage import BidStorageError, get_bid_storage
from app.bids.upload_keys import IMAGE_FORMATS, build_upload_key
from app.config import get_settings
from app.deps import BidderAuth, get_current_bidder
from app.models import Account
from app.models.bid import Attachment
from app.models.bidder import Bidder
from app.security import bidder_token_needs_renewal, create_bidder_token, hash_password, verify_password

router = APIRouter(prefix="/ext", tags=["bidder-ext"])

LIMITER: FailureLimiter = rate_limit.LIMITER

_BAD_LOGIN = "Wrong username or password"
_TAKEN = "Username is taken"
_BAD_CODE = "Invite code is invalid or expired"
# Verified against when the username is unknown or unusable, so every login failure costs one bcrypt check.
_DUMMY_HASH = hash_password("not-a-real-password")


def _keys(username: str, request: Request) -> list[str]:
    return limiter_keys(username, client_ip(request))


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


def _check_rules(body: LoginIn) -> None:
    """400 with the broken username/password rule (after _reserve, so it counts as a failed attempt)."""
    problem = credentials_problem(body.username, body.password)
    if problem:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, problem)


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


def _renewal(auth: BidderAuth) -> dict:
    """{"token": <fresh>} when the presented token is a week old (same ver, new exp), else {}. The extension
    stores any token it gets, so a bidder who keeps using it is never logged out by expiry."""
    if not bidder_token_needs_renewal(auth.claims, datetime.now(timezone.utc)):
        return {}
    return {"token": create_bidder_token(str(auth.bidder.id), auth.bidder.tokenVersion)}


async def register_bidder_row(bidder: Bidder, username: str, password_hash: str) -> bool:
    """Claim the invite in ONE conditional write. False when the code was reused, replaced, or the bidder
    was archived or registered since it was read (the filter, not our earlier read, is the guard). None in the
    filter matches a missing field and an explicit null alike."""
    now = datetime.now(timezone.utc)
    updates = {"username": username, "passwordHash": password_hash, "registeredAt": now, "updatedAt": now}
    bucket = get_settings().BID_STORAGE_BUCKET
    if bucket and bidder.folder:  # counting switches to the folder the extension uploads to
        updates["screenshotFolderUrl"] = folder_url(bucket, bidder.folder)
    result = await Bidder.get_motor_collection().update_one(
        {
            "_id": bidder.id,
            "inviteCodeHash": bidder.inviteCodeHash,
            "registeredAt": None,
            "archivedAt": None,
        },
        {
            "$set": updates,
            "$unset": {"inviteCodeHash": "", "inviteExpiresAt": ""},
        },
    )
    return result.matched_count == 1


@router.get("/latest")
async def latest() -> dict:
    """The newest Bid Track version and its download link (Railway settings), so an older extension can show
    "Update available". No sign-in needed; both null while unset."""
    s = get_settings()
    return {"version": s.EXT_LATEST_VERSION or None, "url": s.EXT_DOWNLOAD_URL or None}


@router.post("/register")
async def register(body: RegisterIn, request: Request) -> dict:
    keys = _keys(body.username, request)
    reserved_at = _reserve(keys)
    _check_rules(body)
    bidder = await Bidder.find_one(Bidder.inviteCodeHash == hash_code(body.inviteCode))
    if bidder is None or not invite_is_live(bidder, datetime.now(timezone.utc)):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, _BAD_CODE)
    if await Bidder.find_one(Bidder.username == body.username) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, _TAKEN)
    password_hash = await asyncio.to_thread(hash_password, body.password)  # bcrypt: off the event loop
    try:
        claimed = await register_bidder_row(bidder, body.username, password_hash)
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
    _check_rules(body)
    bidder = await Bidder.find_one(Bidder.username == body.username)
    usable = bidder is not None and bidder.archivedAt is None and bidder.registeredAt is not None
    stored = bidder.passwordHash if usable and bidder.passwordHash else None
    try:
        password_ok = await asyncio.to_thread(verify_password, body.password, stored or _DUMMY_HASH)
    except ValueError:  # e.g. a password bcrypt refuses to hash: just a wrong password
        password_ok = False
    if not (usable and stored and password_ok):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, _BAD_LOGIN)
    _succeed(keys, reserved_at)
    return await _session(bidder)


@router.get("/me")
async def me(auth: Annotated[BidderAuth, Depends(get_current_bidder)]) -> dict:
    return {**bidder_me(auth.bidder, await _profile_name(auth.bidder)), **_renewal(auth)}


@router.post("/password")
async def change_password(
    body: PasswordChangeIn, request: Request, auth: Annotated[BidderAuth, Depends(get_current_bidder)],
) -> dict:
    """Bumps tokenVersion, so every other browser on this login is logged out; this one gets a fresh token.
    A wrong current password counts toward the same limit as failed logins."""
    bidder = auth.bidder
    keys = _keys(bidder.username or str(bidder.id), request)
    reserved_at = _reserve(keys)
    problem = password_problem(body.newPassword)
    if not problem and body.newPassword == body.currentPassword:
        problem = "Choose a password different from the current one"
    if problem:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, problem)
    try:
        current_ok = await asyncio.to_thread(verify_password, body.currentPassword, bidder.passwordHash or _DUMMY_HASH)
    except ValueError:
        current_ok = False
    if not (bidder.passwordHash and current_ok):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Current password is wrong")
    bidder.passwordHash = await asyncio.to_thread(hash_password, body.newPassword)
    bidder.tokenVersion += 1
    await bidder.save()
    _succeed(keys, reserved_at)
    return await _session(bidder)


_ATTACHMENTS_MAX = 20
_ATTACHMENT_TEXT_MAX = {"path": 1000, "name": 255, "pathError": 200}


class AttachmentIn(BaseModel):
    """A file the bidder attached on the page (see app.models.bid.Attachment). Over-long text is cut rather than
    refused: an upload must never fail because of a file on the page."""

    path: Optional[str] = None
    name: str = ""
    pathError: Optional[str] = None

    @field_validator(*_ATTACHMENT_TEXT_MAX, mode="before")
    @classmethod
    def _cut(cls, v: object, info: ValidationInfo) -> object:
        if not isinstance(v, str):
            return v
        v = v.strip()[: _ATTACHMENT_TEXT_MAX[info.field_name]]
        return v if info.field_name == "name" else v or None


class UploadUrlIn(BaseModel):
    job: str = Field(min_length=1, max_length=200)
    step: int = Field(ge=1, le=500)
    trigger: Literal["step", "next", "submit"]
    capturedAt: AwareDatetime
    # Only stripped here; length and scheme are rules the route checks (the app turns schema errors into a bare
    # "Invalid request").
    jobUrl: Optional[Annotated[str, StringConstraints(strip_whitespace=True)]] = None
    jobTitle: Optional[Annotated[str, StringConstraints(strip_whitespace=True)]] = None
    # Extensions before 3.2.0 send no format and upload PNG.
    format: Literal["png", "webp", "jpeg"] = "png"
    # Sent from 3.3.0, only for a bidder who allowed it; extra entries beyond the cap are dropped.
    attachments: list[AttachmentIn] = []

    @field_validator("attachments", mode="before")
    @classmethod
    def _cap(cls, v: object) -> object:
        return v[:_ATTACHMENTS_MAX] if isinstance(v, list) else v


_JOB_URL_MAX = 2000
_JOB_TITLE_MAX = 300


def _job_url(raw: Optional[str]) -> Optional[str]:
    """400 when too long; None unless it is an http(s) link (it ends up as a clickable href in the review UI)."""
    if raw and len(raw) > _JOB_URL_MAX:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "jobUrl is too long")
    try:
        parts = urlsplit(raw or "")
    except ValueError:
        return None
    return raw if parts.scheme in ("http", "https") and parts.netloc else None


@router.post("/upload-url")
async def upload_url(body: UploadUrlIn, auth: Annotated[BidderAuth, Depends(get_current_bidder)]) -> dict:
    """The backend picks the object path; the extension only PUTs the image to the returned link."""
    bidder = auth.bidder
    job_url = _job_url(body.jobUrl)
    unavailable = HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Uploads are not configured")
    bucket = get_settings().BID_STORAGE_BUCKET
    storage = get_bid_storage()
    if not bucket or not hasattr(storage, "presign_put"):
        raise unavailable
    try:
        key = build_upload_key(
            bidder.folder, await _profile_name(bidder), body.job, body.step, body.trigger,
            body.capturedAt, datetime.now(timezone.utc), body.format,
        )
    except ValueError:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, "capturedAt is not current")
    bid = await record_screenshot(
        bidder, key, body.step, body.trigger, body.capturedAt, job_url,
        (body.jobTitle or "")[:_JOB_TITLE_MAX] or None,
        [Attachment(**a.model_dump()) for a in body.attachments],
    )
    try:
        content_type = IMAGE_FORMATS[body.format][1]
        url = await asyncio.to_thread(storage.presign_put, bucket, key, content_type)
    except BidStorageError:
        raise unavailable from None
    return {
        "url": url,
        "key": key,
        "bidId": str(bid.id),
        "expiresAt": (datetime.now(timezone.utc) + timedelta(seconds=300)).isoformat(),
        "headers": {"Content-Type": content_type},
        **_renewal(auth),
    }


class ConfirmIn(BaseModel):
    key: str = Field(min_length=1, max_length=1000)


@router.post("/uploads/confirm")
async def confirm(body: ConfirmIn, auth: Annotated[BidderAuth, Depends(get_current_bidder)]) -> dict:
    """The extension calls this once the image PUT succeeded; only then does the screenshot count as uploaded."""
    if not await confirm_upload(auth.bidder.id, body.key):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "upload not found")
    return {"ok": True, **_renewal(auth)}
