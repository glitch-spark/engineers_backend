"""One application a bidder recorded with the Bid Track extension: its job folder and screenshots, reviewed later."""

from datetime import datetime
from typing import Literal, Optional

from beanie import PydanticObjectId
from pydantic import BaseModel
from pymongo import IndexModel

from app.models.base import TimestampedDocument


class Screenshot(BaseModel):
    """uploadedAt stays None until the extension confirms the PUT succeeded."""

    key: str
    step: int
    trigger: str
    capturedAt: datetime
    uploadedAt: Optional[datetime] = None


class Bid(TimestampedDocument):
    """Unique per (bidderId, jobKey). Only approved bids count toward a day's bids and pay."""

    bidderId: PydanticObjectId
    ownerId: PydanticObjectId  # Bidder.createdBy at creation; decides review access
    jobKey: str  # <folder>/<YYYY-MM-DD>/<profile>/<job>: the screenshot key without its file name
    jobUrl: Optional[str] = None
    jobTitle: Optional[str] = None
    firstAt: datetime  # earliest capturedAt (naive UTC); decides the bid's report day
    submittedAt: Optional[datetime] = None  # None = no submit screenshot
    screenshots: list[Screenshot] = []
    status: Literal["pending", "approved", "rejected"] = "pending"
    reviewedBy: Optional[PydanticObjectId] = None
    reviewedByName: Optional[str] = None
    reviewedAt: Optional[datetime] = None
    note: Optional[str] = None
    changedSinceReview: bool = False  # a screenshot was added after a decision

    class Settings:
        name = "bids"
        keep_nulls = False
        indexes = [
            IndexModel([("bidderId", 1), ("jobKey", 1)], unique=True),
            IndexModel([("ownerId", 1), ("firstAt", 1)]),
            IndexModel([("bidderId", 1), ("firstAt", 1)]),
            IndexModel([("status", 1), ("firstAt", 1)]),
        ]
