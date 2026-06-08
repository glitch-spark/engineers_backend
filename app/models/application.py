"""Application pipeline model — Kanban-style aggregator linking a bid
(ResumeApplication) and any subsequent interview rounds (Interview) for a
single company + profile combination.

One row per (userId, companyName, jobUrl) — multiple bids to the same
company on the same posting fold into the same Application.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import List, Literal, Optional

from beanie import PydanticObjectId
from pydantic import BaseModel
from pymongo import IndexModel

from app.models.base import TimestampedDocument


# Stage labels mirror InterviewStage where possible so the existing vocabulary
# is reused. `bid_sent` is the pre-interview entry column; `offer` / `rejected`
# / `withdrawn` are terminal outcomes.
class KanbanStage(str, Enum):
    bid_sent = "bid_sent"
    intro = "intro"
    tech = "tech"
    live_coding = "live_coding"
    system_design = "system_design"
    panel = "panel"
    cultural = "cultural"
    final = "final"
    ai_interview = "ai_interview"
    offer = "offer"
    rejected = "rejected"
    withdrawn = "withdrawn"


# Logical ordering used by the auto-bump logic (move-forward never moves back).
STAGE_ORDER: list[str] = [
    "bid_sent", "intro", "tech", "live_coding", "system_design",
    "panel", "cultural", "ai_interview", "final", "offer",
]


ApplicationOutcome = Literal["active", "offer", "rejected", "withdrawn", "no_response"]


class StageEvent(BaseModel):
    stage: str
    at: datetime
    by: Optional[PydanticObjectId] = None  # user that triggered (None = auto)
    source: Optional[str] = None           # 'auto' | 'manual' | 'interview-link' | 'migration'


class Application(TimestampedDocument):
    userId: PydanticObjectId
    accountId: Optional[PydanticObjectId] = None     # profile used
    companyName: str
    companyNameLower: str
    jobUrl: Optional[str] = None
    jobDescription: Optional[str] = None

    # Cross-references — the events that feed this aggregate.
    bidJobIds: List[PydanticObjectId] = []           # ResumeApplication ids
    interviewIds: List[PydanticObjectId] = []        # Interview ids

    stage: str = KanbanStage.bid_sent.value
    outcome: ApplicationOutcome = "active"

    # Confirmation gate. Cards created by in-app résumé bids or manual entry
    # are trusted (confirmed=True). Cards created from an inbound email are
    # AI-proposed and start unconfirmed — they render on the board with a
    # Yes/No control and are excluded from analytics until confirmed.
    confirmed: bool = True
    aiLabel: Optional[str] = None                    # classifier label that placed it
    aiConfidence: Optional[float] = None             # classifier confidence (0-1)

    appliedAt: Optional[datetime] = None             # first bid timestamp
    lastTouchedAt: Optional[datetime] = None         # most recent activity

    notes: str = ""
    stageHistory: List[StageEvent] = []

    archivedAt: Optional[datetime] = None            # set when auto-archived

    class Settings:
        name = "applications"
        indexes = [
            IndexModel([("userId", 1), ("companyNameLower", 1), ("jobUrl", 1)],
                       name="user_company_url"),
            IndexModel([("userId", 1), ("stage", 1), ("lastTouchedAt", -1)],
                       name="user_stage_recent"),
            IndexModel([("userId", 1), ("outcome", 1)], name="user_outcome"),
        ]
