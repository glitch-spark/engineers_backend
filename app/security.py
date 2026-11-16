from datetime import datetime, timedelta, timezone
from typing import Any

from jose import jwt
from passlib.context import CryptContext

from app.config import get_settings

ALGORITHM = "HS256"
BIDDER_TOKEN_DAYS = 30
BIDDER_TOKEN_RENEW_AFTER = timedelta(days=7)

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")


def hash_password(plain: str) -> str:
    return _pwd_context.hash(plain)


def verify_password(plain: str, hashed: str) -> bool:
    return _pwd_context.verify(plain, hashed)


def create_access_token(sub: str, role: str) -> str:
    settings = get_settings()
    expires = datetime.now(timezone.utc) + timedelta(minutes=settings.JWT_EXPIRES_MIN)
    payload: dict[str, Any] = {"sub": sub, "role": role, "exp": expires}
    return jwt.encode(payload, settings.AUTH_SECRET, algorithm=ALGORITHM)


def decode_token(token: str) -> dict[str, Any]:
    settings = get_settings()
    return jwt.decode(token, settings.AUTH_SECRET, algorithms=[ALGORITHM])


def create_bidder_token(bidder_id: str, version: int) -> str:
    """Extension token. typ="bidder" keeps it apart from app tokens; ver ties it to Bidder.tokenVersion;
    iat lets /ext hand out a renewed token once this one is a week old."""
    now = datetime.now(timezone.utc)
    expires = now + timedelta(days=BIDDER_TOKEN_DAYS)
    payload: dict[str, Any] = {"sub": bidder_id, "typ": "bidder", "ver": version, "iat": now, "exp": expires}
    return jwt.encode(payload, get_settings().AUTH_SECRET, algorithm=ALGORITHM)


EXT_OWNER_TOKEN_DAYS = 14  # an owner's extension sign-in ends after two weeks unused (renewed while in use)


def create_ext_owner_token(owner_id: str, profile_id: str) -> str:
    """An owner's extension token, bound to one profile (pid): typ="ext-owner" keeps it apart from app and bidder
    tokens; iat lets /ext/owner renew it like a bidder's."""
    now = datetime.now(timezone.utc)
    payload: dict[str, Any] = {"sub": owner_id, "pid": profile_id, "typ": "ext-owner", "iat": now,
                               "exp": now + timedelta(days=EXT_OWNER_TOKEN_DAYS)}
    return jwt.encode(payload, get_settings().AUTH_SECRET, algorithm=ALGORITHM)


def bidder_token_needs_renewal(claims: dict[str, Any], now: datetime) -> bool:
    """Older than BIDDER_TOKEN_RENEW_AFTER, or issued before tokens carried iat."""
    iat = claims.get("iat")
    if not isinstance(iat, (int, float)):
        return True
    return now - datetime.fromtimestamp(iat, timezone.utc) > BIDDER_TOKEN_RENEW_AFTER
