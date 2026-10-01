from datetime import datetime, timedelta
from typing import Any

from beanie import PydanticObjectId
from pydantic import Field, model_validator
from pymongo import IndexModel

from app.models.base import TimestampedDocument
from app.models.plan_common import ChecklistItems, Counts, WeeklyOverrides, monday_of

_LEGACY_KEYS = ("metrics", "nextInterviewTarget", "status", "content", "result")


def fold_legacy_weekly(data: Any) -> Any:
    """Read a pre Goal-vs-Done weekly plan as the new shape (until migrated)."""
    if not isinstance(data, dict):
        return data
    legacy = any(key in data for key in _LEGACY_KEYS)
    if not legacy and "weekStart" in data:
        return data
    data = dict(data)
    if "weekStart" not in data and data.get("startDate") is not None:
        start = data["startDate"]
        if isinstance(start, str):
            start = datetime.fromisoformat(start.replace("Z", "+00:00"))
        # Plans stored the browser's local Monday midnight (e.g. 04:00Z).
        day = monday_of((start.replace(tzinfo=None) + timedelta(hours=12)).date())
        data["weekStart"] = datetime(day.year, day.month, day.day)
    if legacy and "goal" not in data:
        goal: dict[str, int] = {}
        for metric in data.get("metrics") or []:
            key = metric.get("key") if isinstance(metric, dict) else getattr(metric, "key", None)
            target = metric.get("target") if isinstance(metric, dict) else getattr(metric, "target", 0)
            if key == "bids":
                goal["bidsSelf"] = int(target or 0)
            elif key == "interviews":
                goal["interviewsSelf"] = int(target or 0)
        data["goal"] = goal
        data.setdefault("planNotes", data.get("content") or "")
        data.setdefault("recapNotes", data.get("result") or "")
    for key in _LEGACY_KEYS:
        data.pop(key, None)
    return data


class WeeklyPlan(TimestampedDocument):
    """One person's Goal vs Done for a Monday-start week.

    Done is computed from the week's daily plans and interview records
    (`app.plan_auto`); `doneOverrides` replaces any of it by hand.
    `startDate`/`endDate`/`year`/`weekNumber` are kept for older readers.
    """

    userId: PydanticObjectId
    weekStart: datetime
    weekNumber: int
    year: int
    startDate: datetime
    endDate: datetime
    goal: Counts = Field(default_factory=Counts)
    goalItems: ChecklistItems = []
    planNotes: str = Field(default="", max_length=4000)
    recapNotes: str = Field(default="", max_length=4000)
    doneOverrides: WeeklyOverrides = Field(default_factory=WeeklyOverrides)

    @model_validator(mode="before")
    @classmethod
    def _fold_legacy(cls, data: Any) -> Any:
        return fold_legacy_weekly(data)

    class Settings:
        name = "weeklyplans"
        # Fetch whole documents: by default Beanie projects to the declared
        # fields, which would hide the legacy keys the fold reads.
        projection = None
        indexes = [
            # Not unique: older data may hold duplicates; saves upsert by (userId, weekStart).
            IndexModel([("userId", 1), ("weekStart", 1)]),
            IndexModel([("userId", 1), ("year", 1), ("weekNumber", 1)]),
            IndexModel([("year", 1), ("weekNumber", 1)]),
        ]
