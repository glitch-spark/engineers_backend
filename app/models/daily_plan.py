from datetime import datetime

from beanie import PydanticObjectId
from pymongo import IndexModel

from app.models.base import TimestampedDocument


class DailyPlan(TimestampedDocument):
    """One staff member's note for a calendar day.

    `year` and `weekNumber` use the same calendar-year week as Weekly Plan
    (`app.alerts.app_week_key`) so the shared Report filter matches both lists.
    """

    userId: PydanticObjectId
    date: datetime
    year: int
    weekNumber: int
    today: str = ""
    tomorrow: str = ""

    class Settings:
        name = "dailyplans"
        indexes = [
            IndexModel([("userId", 1), ("date", 1)], unique=True),
            IndexModel([("year", 1), ("weekNumber", 1)]),
        ]
