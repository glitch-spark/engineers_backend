"""Request schema and response shapes for bidders."""

import re
from datetime import datetime, timezone
from typing import Optional

from beanie import PydanticObjectId
from pydantic import BaseModel, field_validator


class BidderIn(BaseModel):
    name: str
    country: Optional[str] = None
    profileId: Optional[str] = None
    rate: float

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("Name is required")
        if len(v) > 100:
            raise ValueError("Name must be at most 100 characters")
        return v

    @field_validator("country")
    @classmethod
    def _country(cls, v: Optional[str]) -> Optional[str]:
        v = (v or "").strip()
        if not v:
            return None
        if len(v) != 2 or not (v.isascii() and v.isalpha()):
            raise ValueError("Use a two-letter country code")
        return v.upper()

    @field_validator("profileId")
    @classmethod
    def _profile_id(cls, v: Optional[str]) -> Optional[str]:
        v = (v or "").strip()
        if not v:
            return None
        if not PydanticObjectId.is_valid(v):
            raise ValueError("Invalid profile")
        return v

    @field_validator("rate")
    @classmethod
    def _rate(cls, v: float) -> float:
        if not 0 <= v <= 1000:
            raise ValueError("Rate must be between 0 and 1000")
        if abs(v * 100 - round(v * 100)) >= 1e-6:
            raise ValueError("Rate must have at most 2 decimals")
        return v


def _iso_utc(dt: Optional[datetime]) -> Optional[str]:
    """UTC ISO string with a Z suffix (as app/routers/interviews.py:_iso_utc). Motor returns naive UTC."""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.isoformat() + "Z"


def bidder_status(b) -> str:
    if b.archivedAt is not None:
        return "archived"
    return "active" if b.registeredAt is not None else "invited"


def bidder_out(b, profile_name: Optional[str]) -> dict:
    return {
        "_id": str(b.id),
        "name": b.name,
        "country": b.country,
        "profileId": str(b.profileId) if b.profileId else None,
        "profileName": profile_name,
        "rate": b.rate,
        "screenshotFolderUrl": b.screenshotFolderUrl,
        "folder": b.folder,
        "status": bidder_status(b),
        "username": b.username,
        "inviteExpiresAt": _iso_utc(b.inviteExpiresAt) if b.inviteCodeHash is not None else None,
        "registeredAt": _iso_utc(b.registeredAt),
        "archivedAt": _iso_utc(b.archivedAt),
        "createdAt": _iso_utc(b.createdAt),
    }


def report_out(r) -> dict:
    return {
        "kind": r.kind,
        "periodKey": r.periodKey,
        "periodStart": _iso_utc(r.periodStart),
        "periodEnd": _iso_utc(r.periodEnd),
        "count": r.count,
        "rate": r.rate,
        "amount": r.amount,
        "error": r.error,
    }


_USERNAME = re.compile(r"^[a-z0-9._-]{3,40}$")


def _username(v: str) -> str:
    v = v.strip().lower()
    if not _USERNAME.match(v):
        raise ValueError("Username must be 3-40 characters: letters, digits, . _ -")
    return v


class RegisterIn(BaseModel):
    inviteCode: str
    username: str
    password: str

    @field_validator("inviteCode")
    @classmethod
    def _code(cls, v: str) -> str:
        if len(v) > 100:
            raise ValueError("Invalid invite code")
        return v

    @field_validator("username")
    @classmethod
    def _user(cls, v: str) -> str:
        return _username(v)

    @field_validator("password")
    @classmethod
    def _password(cls, v: str) -> str:
        if len(v) < 8:
            raise ValueError("Password must be at least 8 characters")
        if len(v.encode()) > 72:  # bcrypt ignores everything past 72 bytes
            raise ValueError("Password must be at most 72 bytes")
        return v


class LoginIn(BaseModel):
    username: str
    password: str

    @field_validator("username")
    @classmethod
    def _user(cls, v: str) -> str:
        return v.strip().lower()  # no format rule: a malformed name is just a wrong name


def bidder_me(b, profile_name: Optional[str]) -> dict:
    return {
        "name": b.name,
        "folder": b.folder,
        "profileName": profile_name,
        "username": b.username,
        "status": bidder_status(b),
    }
