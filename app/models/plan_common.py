"""Goal vs Done building blocks shared by Daily and Weekly plans.

Bids and interviews are flows (summed over a week); profiles and LinkedIn
accounts are stock levels kept per region (a week shows its latest value).
"""

from datetime import date, timedelta
from typing import Annotated, Optional

from pydantic import BaseModel, Field, field_validator

REGIONS = ("US", "EU", "Latam", "Canada", "Asia")
# Profiles without a region on the Profiles page count here.
PROFILE_REGIONS = REGIONS + ("Unassigned",)

MAX_ITEMS = 20
MAX_ITEM_LENGTH = 200


def clean_region_dict(value: Optional[dict], allowed: tuple) -> dict:
    """Validate a region -> count map: known regions, ints >= 0, zeros dropped."""
    out: dict[str, int] = {}
    for region, count in (value or {}).items():
        if region not in allowed:
            raise ValueError(f"unknown region: {region}")
        count = int(count)
        if count < 0:
            raise ValueError(f"{region} count must be 0 or more")
        if count:
            out[region] = count
    return out


Count = Annotated[int, Field(ge=0)]


class Counts(BaseModel):
    bidsSelf: Count = 0
    bidsBidder: Count = 0
    interviewsSelf: Count = 0
    interviewsCaller: Count = 0
    profiles: dict[str, int] = {}
    linkedin: dict[str, int] = {}

    @field_validator("profiles")
    @classmethod
    def _profiles(cls, v: dict) -> dict:
        return clean_region_dict(v, PROFILE_REGIONS)

    @field_validator("linkedin")
    @classmethod
    def _linkedin(cls, v: dict) -> dict:
        return clean_region_dict(v, REGIONS)


class DoneIn(BaseModel):
    """The Done values a user types; the rest of Done is automatic."""

    bidsSelf: Count = 0
    bidsBidder: Count = 0
    linkedin: dict[str, int] = {}

    @field_validator("linkedin")
    @classmethod
    def _linkedin(cls, v: dict) -> dict:
        return clean_region_dict(v, REGIONS)


class DoneOverrides(BaseModel):
    """Hand-typed replacements for automatic daily Done values (None = automatic)."""

    interviewsSelf: Optional[Count] = None
    interviewsCaller: Optional[Count] = None
    profiles: Optional[dict[str, int]] = None

    @field_validator("profiles")
    @classmethod
    def _profiles(cls, v: Optional[dict]) -> Optional[dict]:
        return None if v is None else clean_region_dict(v, PROFILE_REGIONS)


class WeeklyOverrides(BaseModel):
    """Hand-typed replacements for any automatic weekly Done value (None = automatic)."""

    bidsSelf: Optional[Count] = None
    bidsBidder: Optional[Count] = None
    interviewsSelf: Optional[Count] = None
    interviewsCaller: Optional[Count] = None
    profiles: Optional[dict[str, int]] = None
    linkedin: Optional[dict[str, int]] = None

    @field_validator("profiles")
    @classmethod
    def _profiles(cls, v: Optional[dict]) -> Optional[dict]:
        return None if v is None else clean_region_dict(v, PROFILE_REGIONS)

    @field_validator("linkedin")
    @classmethod
    def _linkedin(cls, v: Optional[dict]) -> Optional[dict]:
        return None if v is None else clean_region_dict(v, REGIONS)


class ChecklistItem(BaseModel):
    text: str
    done: bool = False

    @field_validator("text")
    @classmethod
    def _text(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("checklist items need text")
        if len(v) > MAX_ITEM_LENGTH:
            raise ValueError(f"checklist items are limited to {MAX_ITEM_LENGTH} characters")
        return v


def check_items(items: list) -> list:
    if len(items) > MAX_ITEMS:
        raise ValueError(f"at most {MAX_ITEMS} checklist items")
    return items


def next_working_day(d: date) -> date:
    """The next Monday–Saturday day after `d` (Saturday and Sunday → Monday)."""
    nxt = d + timedelta(days=1)
    return nxt + timedelta(days=1) if nxt.weekday() == 6 else nxt


def monday_of(d: date) -> date:
    """Monday of the Mon–Sun week holding `d` (Sunday belongs to the week that just ended)."""
    return d - timedelta(days=d.weekday())
