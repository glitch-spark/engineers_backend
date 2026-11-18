"""One identifiable job application (a Bid Track bid and/or a tailored resume) with the interview it led to.
Precomputed by app/applications; see docs/superpowers/specs/2026-11-19-applications-insights-design.md."""

from datetime import datetime
from typing import Literal, Optional

from beanie import PydanticObjectId
from pymongo import IndexModel

from app.models.base import TimestampedDocument


class Application(TimestampedDocument):
    key: str  # "<accountId>:<jobKey>", or "src:<collection>:<id>" for a source without a job link
    accountId: Optional[PydanticObjectId] = None
    ownerId: PydanticObjectId
    region: Optional[str] = None
    jobKey: Optional[str] = None
    board: Optional[str] = None  # registrable domain of the job link
    appliedAt: datetime  # earliest source time (naive UTC)
    title: Optional[str] = None
    area: Optional[str] = None
    profileAgeDays: Optional[int] = None  # appliedAt - the profile's createdAt, whole days
    by: Literal["self", "bidder"] = "self"
    bidderId: Optional[PydanticObjectId] = None
    bidStatus: Optional[str] = None
    score: Optional[int] = None
    scoreBand: Optional[str] = None
    promptId: Optional[str] = None
    promptName: Optional[str] = None
    templateName: Optional[str] = None
    interviewId: Optional[PydanticObjectId] = None
    firstRoundAt: Optional[datetime] = None
    daysToInterview: Optional[int] = None
    furthest: Optional[str] = None
    result: Optional[str] = None
    sources: list[str] = []
    syncedAt: datetime

    class Settings:
        name = "applications"
        keep_nulls = False
        indexes = [
            IndexModel([("key", 1)], unique=True),
            IndexModel([("ownerId", 1), ("appliedAt", 1)]),
            IndexModel([("accountId", 1), ("appliedAt", 1)]),
            IndexModel([("interviewId", 1)]),
        ]
