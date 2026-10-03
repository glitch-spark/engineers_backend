"""A bidder whose daily and weekly bid counts are reported to Slack."""

from datetime import datetime
from typing import Optional

from beanie import PydanticObjectId
from pymongo import IndexModel

from app.models.base import TimestampedDocument


class Bidder(TimestampedDocument):
    """Owner-only (createdBy). Delete is soft: archivedAt excludes it from lists and reports."""

    createdBy: PydanticObjectId
    name: str
    country: Optional[str] = None
    profileId: Optional[PydanticObjectId] = None
    rate: float  # USD per bid
    screenshotFolderUrl: str
    archivedAt: Optional[datetime] = None

    class Settings:
        name = "bidders"
        indexes = [
            IndexModel([("createdBy", 1), ("archivedAt", 1)]),
        ]
