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
    # Extension login: folder is the bidder's top-level prefix in the bucket; invite/registration fields
    # are None until used. tokenVersion invalidates issued JWTs when bumped.
    folder: Optional[str] = None
    inviteCodeHash: Optional[str] = None
    inviteExpiresAt: Optional[datetime] = None
    username: Optional[str] = None
    passwordHash: Optional[str] = None
    registeredAt: Optional[datetime] = None
    tokenVersion: int = 0

    class Settings:
        name = "bidders"
        # Omit None fields: real MongoDB indexes an explicit null in a sparse unique index,
        # so a second bidder without a username would collide.
        keep_nulls = False
        indexes = [
            IndexModel([("createdBy", 1), ("archivedAt", 1)]),
            IndexModel([("folder", 1)], unique=True, sparse=True),
            IndexModel([("username", 1)], unique=True, sparse=True),
            IndexModel([("inviteCodeHash", 1)], unique=True, sparse=True),
        ]
