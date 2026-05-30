from datetime import datetime
from typing import List, Literal, Optional

from beanie import PydanticObjectId
from pydantic import BaseModel
from pymongo import IndexModel

from app.models.base import TimestampedDocument

WeeklyPlanStatus = Literal["planned", "reviewed"]

# Core metric catalog — recommended defaults seeded into every new plan.
# Users may add custom rows on top. Keys are stable so the summary
# aggregate + charts stay consistent across weeks and users.
CORE_METRICS: list[dict] = [
    {"key": "bids", "label": "Job applies"},
    {"key": "interviews", "label": "Interviews"},
    {"key": "resume", "label": "Resume/profile maintenance"},
    {"key": "outreach", "label": "Founder outreaches"},
]


MetricUnit = Literal["count", "hours"]


class WeeklyMetric(BaseModel):
    key: str
    label: str
    target: int = 0
    actual: int = 0
    # 'count' = discrete things (bids, interviews). 'hours' = time
    # commitments (e.g. '1hr/day' study). Lets the UI render units and
    # keeps time goals out of count-based core metrics.
    unit: MetricUnit = "count"


class WeeklyPlan(TimestampedDocument):
    userId: PydanticObjectId
    weekNumber: int
    year: int
    startDate: datetime
    endDate: datetime

    # Structured progress metrics (target set at week start, actual at week
    # end). Empty on legacy rows — UI renders gracefully.
    metrics: List[WeeklyMetric] = []
    # Forward-looking interview goal for NEXT week, set during the
    # end-of-week review. Used to pre-seed next week's interviews target.
    nextInterviewTarget: Optional[int] = None
    status: WeeklyPlanStatus = "planned"

    # Free-text notes. `content` = start-of-week plan, `result` =
    # end-of-week recap. Kept as-is for back-compat (no migration).
    content: str = ""
    result: str = ""

    class Settings:
        name = "weeklyplans"
        indexes = [
            IndexModel([("userId", 1), ("year", 1), ("weekNumber", 1)]),
            IndexModel([("year", 1), ("weekNumber", 1)]),
        ]
