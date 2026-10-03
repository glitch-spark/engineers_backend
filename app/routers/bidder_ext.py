"""Routes the Bid Track extension calls: register with an invite code, log in, who am I."""

from datetime import datetime, timezone
from typing import Annotated, Optional

from beanie.exceptions import RevisionIdWasChanged
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pymongo.errors import DuplicateKeyError

from app.bids import rate_limit
from app.bids.invites import hash_code, invite_is_live
from app.bids.rate_limit import FailureLimiter, client_ip
from app.bids.schemas import LoginIn, RegisterIn, bidder_me
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


def _check_limit(keys: list[str]) -> None:
    wait = LIMITER.retry_after(keys, datetime.now(timezone.utc))
    if wait is not None:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many attempts — try again later",
            headers={"Retry-After": str(wait)},
        )


def _fail(keys: list[str], status_code: int, detail: str) -> HTTPException:
    LIMITER.fail(keys, datetime.now(timezone.utc))
    return HTTPException(status_code=status_code, detail=detail)


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
    result = await Bidder.get_motor_collection().update_one(
        {
            "_id": bidder.id,
            "inviteCodeHash": bidder.inviteCodeHash,
            "registeredAt": {"$exists": False},
            "archivedAt": {"$exists": False},
        },
        {
            "$set": {"username": username, "passwordHash": password_hash, "registeredAt": now, "updatedAt": now},
            "$unset": {"inviteCodeHash": "", "inviteExpiresAt": ""},
        },
    )
    return result.matched_count == 1


@router.post("/register")
async def register(body: RegisterIn, request: Request) -> dict:
    keys = _keys(body.username, request)
    _check_limit(keys)
    bidder = await Bidder.find_one(Bidder.inviteCodeHash == hash_code(body.inviteCode))
    if bidder is None or not invite_is_live(bidder, datetime.now(timezone.utc)):
        raise _fail(keys, status.HTTP_400_BAD_REQUEST, _BAD_CODE)
    if await Bidder.find_one(Bidder.username == body.username) is not None:
        raise _fail(keys, status.HTTP_409_CONFLICT, _TAKEN)
    try:
        claimed = await register_bidder_row(bidder, body.username, hash_password(body.password))
    except (DuplicateKeyError, RevisionIdWasChanged):  # a concurrent registration took the username
        raise _fail(keys, status.HTTP_409_CONFLICT, _TAKEN)
    if not claimed:
        raise _fail(keys, status.HTTP_400_BAD_REQUEST, _BAD_CODE)
    LIMITER.clear(f"user:{body.username}")
    return await _session(await Bidder.get(bidder.id))


@router.post("/login")
async def login(body: LoginIn, request: Request) -> dict:
    keys = _keys(body.username, request)
    _check_limit(keys)
    bidder = await Bidder.find_one(Bidder.username == body.username) if body.username else None
    usable = bidder is not None and bidder.archivedAt is None and bidder.registeredAt is not None
    stored = bidder.passwordHash if usable and bidder.passwordHash else None
    try:
        password_ok = verify_password(body.password, stored or _DUMMY_HASH)
    except ValueError:  # e.g. a password bcrypt refuses to hash: just a wrong password
        password_ok = False
    if not (usable and stored and password_ok):
        raise _fail(keys, status.HTTP_401_UNAUTHORIZED, _BAD_LOGIN)
    LIMITER.clear(f"user:{body.username}")
    return await _session(bidder)


@router.get("/me")
async def me(bidder: Annotated[Bidder, Depends(get_current_bidder)]) -> dict:
    return bidder_me(bidder, await _profile_name(bidder))
