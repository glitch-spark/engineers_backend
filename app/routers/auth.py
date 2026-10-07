import asyncio
import re
from datetime import datetime, timezone
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.bids.rate_limit import FailureLimiter, client_ip, limiter_keys
from app.bids.schemas import password_problem
from app.deps import get_current_user
from app.models import UserExtra
from app.security import create_access_token, hash_password, verify_password

router = APIRouter(prefix="/auth", tags=["auth"])

# Staff logins get their own counters (same rules as /ext): a bidder and a staff user may share a username.
LIMITER = FailureLimiter()

_BAD_LOGIN = "invalid credentials"
# Verified against when the user is unknown or has no usable hash, so every login failure costs one bcrypt check.
_DUMMY_HASH = hash_password("not-a-real-password")


class RegisterIn(BaseModel):
    username: str
    email: str
    password: str = Field(max_length=1024)


class LoginIn(BaseModel):
    """Length caps only bound memory and the limiter keys."""

    username: str = Field(max_length=200)
    password: str = Field(max_length=1024)


class UserOut(BaseModel):
    id: str
    name: Optional[str] = None
    email: Optional[str] = None
    role: str
    image: Optional[str] = None
    createdAt: Optional[datetime] = None
    lastLoginAt: Optional[datetime] = None


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    user: UserOut


def _as_utc(dt: Optional[datetime]) -> Optional[datetime]:
    # Mongo hands back naive UTC datetimes; tag them so the JSON carries "+00:00"
    # and browsers don't parse them as local time.
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _user_to_out(u: UserExtra) -> UserOut:
    return UserOut(
        id=str(u.id), name=u.name, email=u.email, role=u.role,
        image=getattr(u, "image", None) or None,
        createdAt=_as_utc(getattr(u, "createdAt", None)),
        lastLoginAt=_as_utc(getattr(u, "lastLoginAt", None)),
    )


def _ci_eq(field: str, value: str) -> dict:
    # Escape regex specials so user input cannot be interpreted as a pattern.
    return {field: {"$regex": f"^{re.escape(value)}$", "$options": "i"}}


def _reserve(keys: list[str]) -> datetime:
    """Count the attempt as a failure before any await; a success gives it back. 429 at the limit."""
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
    """Successes never count toward the IP limit (shared office IPs); clear the username."""
    user_key, ip_key = keys
    LIMITER.release([ip_key], reserved_at)
    LIMITER.clear(user_key)


def _verify(plain: str, stored: Optional[str]) -> bool:
    """One bcrypt check whatever the stored value. One that isn't a hash (admin-created users hold
    "placeholder") or a password bcrypt refuses is a failed login, still paid for with a dummy check."""
    try:
        return verify_password(plain, stored or _DUMMY_HASH) and bool(stored)
    except ValueError:  # passlib's UnknownHashError is a ValueError
        try:
            verify_password(plain, _DUMMY_HASH)
        except ValueError:
            pass
        return False


async def _password_ok(plain: str, stored: Optional[str]) -> bool:
    return await asyncio.to_thread(_verify, plain, stored)  # bcrypt: off the event loop


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(body: RegisterIn) -> dict:
    if not body.username or not body.email or not body.password:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Missing fields")
    problem = password_problem(body.password)
    if problem:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=problem)

    email_lower = body.email.lower()
    existing = await UserExtra.find_one(
        {"$or": [_ci_eq("name", body.username), _ci_eq("email", email_lower)]}
    )
    if existing:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Try another username or email",
        )

    count = await UserExtra.count()
    is_first = count == 0
    role = "admin" if is_first else "staff"

    user = UserExtra(
        name=body.username,
        email=email_lower,
        hashedPassword=await asyncio.to_thread(hash_password, body.password),
        role=role,
        isActive=is_first,  # bootstrap admin auto-active; everyone else awaits approval
        # Default-on for staff so the leaderboard is populated. Admins
        # default-off (workspace owners shouldn't auto-display on rankings).
        leaderboardOptIn=not is_first,
    )
    await user.insert()
    return {"ok": True, "pendingApproval": not is_first}


@router.post("/login", response_model=TokenOut)
async def login(body: LoginIn, request: Request) -> TokenOut:
    username = body.username.strip().lower()
    keys = limiter_keys(username, client_ip(request))
    reserved_at = _reserve(keys)
    if not username or not body.password:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=_BAD_LOGIN)

    user = await UserExtra.find_one(
        {"$or": [_ci_eq("name", username), _ci_eq("email", username)]}
    )
    stored = user.hashedPassword if user is not None else None
    if not await _password_ok(body.password, stored):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=_BAD_LOGIN)
    _succeed(keys, reserved_at)

    if not user.isActive:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Account pending admin approval",
        )

    user.lastLoginAt = datetime.utcnow()
    await user.set({UserExtra.lastLoginAt: user.lastLoginAt})

    token = create_access_token(sub=str(user.id), role=user.role)
    return TokenOut(access_token=token, token_type="bearer", user=_user_to_out(user))


@router.get("/me", response_model=UserOut)
async def me(user: Annotated[UserExtra, Depends(get_current_user)]) -> UserOut:
    return _user_to_out(user)
