from datetime import datetime
from typing import Literal, Optional

from beanie import PydanticObjectId
from pydantic import BaseModel
from pymongo import IndexModel

from app.models.base import TimestampedDocument


class DailyPlanItem(BaseModel):
    """A counted line a staff member adds to a regular plan, e.g. `Cold emails: 5`."""

    label: str
    value: int = 0


class DailyPlan(TimestampedDocument):
    """One staff member's note for a calendar day.

    `year` and `weekNumber` use the same calendar-year week as Weekly Plan
    (`app.alerts.app_week_key`) so the shared Report filter matches both lists.

    `planType` "custom" is the free-text today/tomorrow note. "regular" records
    the day's counts (bids split by who sent them, interviews done and new
    invitations) plus any extra counted items; those feed `/daily-plans/stats`.
    Plans written before the toggle have no `planType` and load as "custom".
    """

    userId: PydanticObjectId
    date: datetime
    year: int
    weekNumber: int
    planType: Literal["custom", "regular"] = "custom"
    today: str = ""
    tomorrow: str = ""
    bidsHandsOn: int = 0
    bidsByBidder: int = 0
    interviewsDone: int = 0
    interviewsNew: int = 0
    customItems: list[DailyPlanItem] = []
    # Bot post in SLACK_DAILY_PLAN_CHANNEL_ID, kept so edits update it in place.
    slackChannel: Optional[str] = None
    slackTs: Optional[str] = None

    class Settings:
        name = "dailyplans"
        indexes = [
            IndexModel([("userId", 1), ("date", 1)], unique=True),
            IndexModel([("year", 1), ("weekNumber", 1)]),
        ]
