from datetime import datetime
from typing import Any, Optional

from beanie import PydanticObjectId
from pydantic import Field, model_validator
from pymongo import IndexModel

from app.models.base import TimestampedDocument
from app.models.plan_common import ChecklistItems, Counts, DoneOverrides

# Fields of the pre Goal-vs-Done shape. `planBids*` / `planInterviewsScheduled`
# / `tomorrowItems` describe the NEXT day, so the fold drops them here and
# scripts/migrate_plans_goal_done.py moves them onto that day's goal.
_LEGACY_KEYS = (
    "planType", "today", "tomorrow",
    "bidsHandsOn", "bidsByBidder", "interviewsDone", "interviewsNew", "todayItems",
    "planBidsHandsOn", "planBidsByBidder", "planInterviewsScheduled", "tomorrowItems",
    "customItems",
)


def _legacy_today_items(data: dict) -> list[str]:
    items = [str(i) for i in (data.get("todayItems") or []) if str(i).strip()]
    if not items:
        # Regular plans saved before items were plain text stored
        # `customItems: [{label, value}]`.
        items = [
            f"{item['label'].strip()}: {item.get('value') or 0}"
            for item in (data.get("customItems") or [])
            if isinstance(item, dict) and (item.get("label") or "").strip()
        ]
    return items


def fold_legacy_daily(data: Any) -> Any:
    """Read a pre Goal-vs-Done daily plan as the new shape (until migrated)."""
    if not isinstance(data, dict) or "done" in data:
        return data
    if not any(key in data for key in _LEGACY_KEYS):
        return data
    data = dict(data)
    bids_self = int(data.get("bidsHandsOn") or 0)
    bids_bidder = int(data.get("bidsByBidder") or 0)
    interviews = int(data.get("interviewsDone") or 0)
    items = _legacy_today_items(data)
    text = "\n\n".join(
        part.strip() for part in (data.get("today") or "", data.get("tomorrow") or "") if part.strip()
    )
    data["done"] = {"bidsSelf": bids_self, "bidsBidder": bids_bidder}
    # A regular plan's count was all of that day's interviews: keep it exact
    # (no automatic caller rounds on top). Free-text plans counted nothing.
    regular = data.get("planType") == "regular"
    data["doneOverrides"] = {"interviewsSelf": interviews, "interviewsCaller": 0} if regular else {}
    data["goalItems"] = [{"text": item[:200], "done": True} for item in items]
    if text and not data.get("notes"):
        data["notes"] = text
    if bids_self or bids_bidder or interviews or items or text:
        data.setdefault("loggedAt", data.get("createdAt"))
    for key in _LEGACY_KEYS:
        data.pop(key, None)
    return data


class DailyPlan(TimestampedDocument):
    """One person's Goal vs Done for a calendar day.

    `goal` and `goalItems` are set the evening before (or that morning);
    `done` holds what the person types (bids, LinkedIn) plus the profiles
    snapshot taken when the day is logged. Interviews Done is automatic from
    interview records unless `doneOverrides` says otherwise. `loggedAt` is
    set the first time Done is saved — a goal-only entry for a future day
    has none.

    `year` and `weekNumber` use the calendar-year week of
    `app.alerts.app_week_key` like before.
    """

    userId: PydanticObjectId
    date: datetime
    year: int
    weekNumber: int
    goal: Counts = Field(default_factory=Counts)
    goalItems: ChecklistItems = []
    done: Counts = Field(default_factory=Counts)
    doneOverrides: DoneOverrides = Field(default_factory=DoneOverrides)
    notes: str = Field(default="", max_length=2000)
    loggedAt: Optional[datetime] = None
    # Bot post in SLACK_DAILY_PLAN_CHANNEL_ID, kept so edits update it in place.
    slackChannel: Optional[str] = None
    slackTs: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def _fold_legacy(cls, data: Any) -> Any:
        return fold_legacy_daily(data)

    class Settings:
        name = "dailyplans"
        # Fetch whole documents: by default Beanie projects to the declared
        # fields, which would hide the legacy keys the fold reads.
        projection = None
        indexes = [
            IndexModel([("userId", 1), ("date", 1)], unique=True),
            IndexModel([("year", 1), ("weekNumber", 1)]),
        ]
