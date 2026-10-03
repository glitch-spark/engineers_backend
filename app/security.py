from datetime import datetime, timedelta, timezone
from typing import Any

from jose import jwt
from passlib.context import CryptContext

from app.config import get_settings

ALGORITHM = "HS256"
BIDDER_TOKEN_DAYS = 30

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
    """Extension token. typ="bidder" keeps it apart from app tokens; ver ties it to Bidder.tokenVersion."""
    expires = datetime.now(timezone.utc) + timedelta(days=BIDDER_TOKEN_DAYS)
    payload: dict[str, Any] = {"sub": bidder_id, "typ": "bidder", "ver": version, "exp": expires}
    return jwt.encode(payload, get_settings().AUTH_SECRET, algorithm=ALGORITHM)
