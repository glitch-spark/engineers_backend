from datetime import datetime
from enum import Enum
from typing import List, Optional

from beanie import PydanticObjectId
from pydantic import BaseModel, field_validator, model_validator
from pymongo import IndexModel

from app.models.base import TimestampedDocument


class InterviewStage(str, Enum):
    intro = "intro"
    tech = "tech"  # legacy / generic; prefer tech sub-stages below
    tech_round_1 = "tech_round_1"
    tech_round_2 = "tech_round_2"
    live_coding = "live_coding"
    system_design = "system_design"
    home_assessment = "home_assessment"
    panel = "panel"
    cultural = "cultural"
    final = "final"
    rejected = "rejected"
    ai_interview = "ai_interview"


# Legacy stage strings still present in old documents; mapped on read so the
# document loads against the trimmed enum. Writes must use the new values.
_LEGACY_STAGE_MAP = {
    "tech1": InterviewStage.tech_round_1,
    "tech2": InterviewStage.tech_round_2,
    "hiring_manager": InterviewStage.final,
    "offer": InterviewStage.final,
    "others": InterviewStage.intro,
}


class InterviewStatus(str, Enum):
    scheduled = "scheduled"
    completed = "completed"
    passed = "passed"
    failed = "failed"
    no_show = "no_show"
    rescheduled = "rescheduled"
    canceled = "canceled"


class InterviewStageEvent(BaseModel):
    stage: str
    at: datetime
    by: Optional[PydanticObjectId] = None
    source: Optional[str] = None  # 'create' | 'manual' | 'board' | 'edit' | 'seed'
    # Interview scheduled date for this round (when they sat this stage).
    scheduledAt: Optional[datetime] = None


class Interview(TimestampedDocument):
    accountId: PydanticObjectId
    resumeId: Optional[PydanticObjectId] = None  # legacy; no longer surfaced in UI
    createdBy: PydanticObjectId
    scheduledAt: datetime
    endsAt: Optional[datetime] = None
    stage: Optional[InterviewStage] = None
    status: Optional[InterviewStatus] = None
    companyName: Optional[str] = None
    interviewerName: Optional[str] = None
    appliedPosition: Optional[str] = None
    jobUrl: Optional[str] = None
    mainTechStack: Optional[str] = None
    transcript: str = ""
    note: str = ""
    stageHistory: List[InterviewStageEvent] = []

    @field_validator("stage", mode="before")
    @classmethod
    def _coerce_legacy_stage(cls, v):
        if isinstance(v, str) and v in _LEGACY_STAGE_MAP:
            return _LEGACY_STAGE_MAP[v]
        return v

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_fields(cls, data):
        # Old documents stored interviewer name under `interviewName`.
        if isinstance(data, dict):
            if data.get("interviewerName") in (None, "") and data.get("interviewName"):
                data["interviewerName"] = data["interviewName"]
        return data

    class Settings:
        name = "interviews"
        indexes = [
            IndexModel([("scheduledAt", -1)]),
            IndexModel([("createdBy", 1)]),
            IndexModel([("accountId", 1)]),
            IndexModel([("stage", 1)]),
            IndexModel([("status", 1)]),
        ]
