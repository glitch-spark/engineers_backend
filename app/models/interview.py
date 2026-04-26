from datetime import datetime
from typing import Optional

from beanie import PydanticObjectId
from pymongo import IndexModel

from app.models.base import TimestampedDocument


class Interview(TimestampedDocument):
    accountId: PydanticObjectId
    resumeId: Optional[PydanticObjectId] = None
    createdBy: PydanticObjectId
    scheduledAt: datetime
    endsAt: Optional[datetime] = None
    stage: str
    jobDescription: str = ""
    transcript: str = ""
    note: str = ""

    class Settings:
        name = "interviews"
        indexes = [
            IndexModel([("scheduledAt", -1)]),
            IndexModel([("createdBy", 1)]),
            IndexModel([("accountId", 1)]),
            IndexModel([("stage", 1)]),
        ]
