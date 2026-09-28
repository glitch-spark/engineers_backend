from datetime import datetime
from typing import Any, Literal, Optional

from beanie import PydanticObjectId
from pydantic import model_validator
from pymongo import IndexModel

from app.models.base import TimestampedDocument


def fold_counted_items(data: Any) -> Any:
    """Regular plans saved before items were plain text stored
    `customItems: [{label, value}]`; read them as today's items."""
    if not isinstance(data, dict) or "customItems" not in data:
        return data
    data = dict(data)
    legacy = data.pop("customItems") or []
    if not data.get("todayItems"):
        data["todayItems"] = [
            f"{item['label'].strip()}: {item.get('value') or 0}"
            for item in legacy
            if isinstance(item, dict) and (item.get("label") or "").strip()
        ]
    return data


class DailyPlan(TimestampedDocument):
    """One staff member's note for a calendar day.

    `year` and `weekNumber` use the same calendar-year week as Weekly Plan
    (`app.alerts.app_week_key`) so the shared Report filter matches both lists.

    `planType` "custom" is the free-text today/tomorrow note. "regular" covers
    the same two days with counts: what was done on `date` (bids split by who
    sent them, interviews done, new invitations) and the plan for the next day
    (bids, scheduled interviews), each with plain-text items. The counts feed
    `/daily-plans/stats`. Plans written before the toggle have no `planType`
    and load as "custom".
    """

    userId: PydanticObjectId
    date: datetime
    year: int
    weekNumber: int
    planType: Literal["custom", "regular"] = "custom"
    today: str = ""
    tomorrow: str = ""
    # Done on `date`.
    bidsHandsOn: int = 0
    bidsByBidder: int = 0
    interviewsDone: int = 0
    interviewsNew: int = 0
    todayItems: list[str] = []
    # Planned for the day after `date`.
    planBidsHandsOn: int = 0
    planBidsByBidder: int = 0
    planInterviewsScheduled: int = 0
    tomorrowItems: list[str] = []
    # Bot post in SLACK_DAILY_PLAN_CHANNEL_ID, kept so edits update it in place.
    slackChannel: Optional[str] = None
    slackTs: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def _fold_counted_items(cls, data: Any) -> Any:
        # The next save writes the new shape, so no migration is needed.
        return fold_counted_items(data)

    class Settings:
        name = "dailyplans"
        indexes = [
            IndexModel([("userId", 1), ("date", 1)], unique=True),
            IndexModel([("year", 1), ("weekNumber", 1)]),
        ]
