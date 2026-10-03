"""Request schema and response shapes for bidders."""

from datetime import datetime, timezone
from typing import Optional

from beanie import PydanticObjectId
from pydantic import BaseModel, field_validator

from app.bids.storage import parse_folder_url


class BidderIn(BaseModel):
    name: str
    country: Optional[str] = None
    profileId: Optional[str] = None
    rate: float
    screenshotFolderUrl: str

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

    @field_validator("screenshotFolderUrl")
    @classmethod
    def _folder_url(cls, v: str) -> str:
        v = v.strip()
        try:
            parse_folder_url(v)
        except ValueError as e:
            if "points at a file" in str(e):
                raise ValueError("Use the bidder's folder, not a single screenshot (e.g. s3://bid-screenshots/puma/)") from None
            raise ValueError("Use an s3://, amazonaws.com or backblazeb2.com folder URL") from None
        return v


def _iso_utc(dt: Optional[datetime]) -> Optional[str]:
    """UTC ISO string with a Z suffix (as app/routers/interviews.py:_iso_utc). Motor returns naive UTC."""
    if dt is None:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.isoformat() + "Z"


def bidder_out(b, profile_name: Optional[str]) -> dict:
    return {
        "_id": str(b.id),
        "name": b.name,
        "country": b.country,
        "profileId": str(b.profileId) if b.profileId else None,
        "profileName": profile_name,
        "rate": b.rate,
        "screenshotFolderUrl": b.screenshotFolderUrl,
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
