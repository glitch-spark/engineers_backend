from datetime import datetime

from beanie import PydanticObjectId
from pymongo import IndexModel

from app.models.base import TimestampedDocument


class WeeklyPlan(TimestampedDocument):
    userId: PydanticObjectId
    weekNumber: int
    year: int
    startDate: datetime
    endDate: datetime
    content: str
    result: str = ""

    class Settings:
        name = "weeklyplans"
        indexes = [
            IndexModel([("userId", 1), ("year", 1), ("weekNumber", 1)]),
            IndexModel([("year", 1), ("weekNumber", 1)]),
        ]
