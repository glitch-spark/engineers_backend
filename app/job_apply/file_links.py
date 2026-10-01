"""Signed, login-free links to one resume PDF, for the exported Google Sheet.

A link names one file (a tailored resume = ResumeApplication id, or an uploaded resume = profile + resume id),
is signed with AUTH_SECRET for a separate audience (a login token can't be used as a link), and expires after
LINK_DAYS.
"""
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

from jose import JWTError, jwt

from app.config import get_settings

AUDIENCE = "job-apply-file"
LINK_DAYS = 30
Kind = Literal["tailored", "resume"]


class BadLink(Exception):
    pass


def make_token(kind: Kind, file_id: str, account_id: Optional[str] = None, now: Optional[datetime] = None) -> str:
    now = now or datetime.now(timezone.utc)
    payload = {
        "aud": AUDIENCE,
        "k": kind,
        "i": file_id,
        "a": account_id,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(days=LINK_DAYS)).timestamp()),
    }
    return jwt.encode(payload, get_settings().AUTH_SECRET, algorithm="HS256")


def read_token(token: str) -> dict:
    try:
        payload = jwt.decode(token, get_settings().AUTH_SECRET, algorithms=["HS256"], audience=AUDIENCE)
    except JWTError as exc:
        raise BadLink(str(exc)) from exc
    if payload.get("k") not in ("tailored", "resume") or not payload.get("i"):
        raise BadLink("not a file link")
    return {"kind": payload["k"], "id": payload["i"], "accountId": payload.get("a")}


def file_url(kind: Kind, file_id: str, account_id: Optional[str] = None) -> str:
    base = get_settings().APP_BASE_URL.rstrip("/")
    return f"{base}/job-applies/files/{make_token(kind, file_id, account_id)}"
