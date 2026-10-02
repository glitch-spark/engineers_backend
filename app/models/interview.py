import hashlib
from datetime import datetime
from datetime import timezone as dt_timezone
from enum import Enum
from typing import Any, List, Literal, Optional
from uuid import uuid4

from beanie import PydanticObjectId
from pydantic import BaseModel, Field, PrivateAttr, field_validator, model_validator
from pymongo import IndexModel

from app.interview_normalize import normalize_status, resolve_interview
from app.models.base import TimestampedDocument


class InterviewStage(str, Enum):
    intro = "intro"
    tech_round_1 = "tech_round_1"
    tech_round_2 = "tech_round_2"
    live_coding = "live_coding"
    system_design = "system_design"
    home_assessment = "home_assessment"
    cultural = "cultural"
    panel = "panel"
    final = "final"
    ai_interview = "ai_interview"


class InterviewStatus(str, Enum):
    """Per-round status. Rejected/canceled are statuses, never stages; legacy
    values (failed, no_show, rescheduled) are mapped by normalize_status."""

    scheduled = "scheduled"
    completed = "completed"
    passed = "passed"
    rejected = "rejected"
    canceled = "canceled"


def stage_event_id(stage: str, at: Any, scheduled_at: Any = None) -> str:
    """Deterministic id for legacy entries saved before stages had ids, so the
    same entry keeps the same id across reads until it is persisted."""
    seed = f"{stage}|{at}|{scheduled_at}"
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:24]


CallerMethod = Literal["video", "phone_hushed", "phone_slynumber"]


class InterviewCaller(BaseModel):
    """Optional request for a caller (a coworker) to join this interview."""

    enabled: bool = False
    callerName: str = "TBD"
    # Exact start in UTC; None means the time is still TBD.
    startsAt: Optional[datetime] = None
    timezone: Optional[str] = None  # legacy, no longer written: Slack shows each reader their own time
    method: Optional[CallerMethod] = None
    methodValue: str = ""
    coworkerIds: List[PydanticObjectId] = []
    slackChannelTs: Optional[str] = None

    @field_validator("startsAt", mode="after")
    @classmethod
    def _utc_aware(cls, v: Optional[datetime]) -> Optional[datetime]:
        # Mongo returns naive datetimes; tag them UTC so JSON carries the offset.
        if v is not None and v.tzinfo is None:
            return v.replace(tzinfo=dt_timezone.utc)
        return v


class InterviewStageEvent(BaseModel):
    id: str = Field(default_factory=lambda: uuid4().hex)
    stage: str
    at: datetime
    by: Optional[PydanticObjectId] = None
    source: Optional[str] = None  # 'create' | 'manual' | 'board' | 'edit' | 'seed'
    # Interview scheduled date for this round (when they sat this stage).
    scheduledAt: Optional[datetime] = None
    status: Optional[InterviewStatus] = None
    transcript: str = ""
    note: str = ""
    # Per-round details (phase 2). The interview-level interviewerName/caller
    # mirror the latest round's for one release.
    interviewerName: Optional[str] = None
    endsAt: Optional[datetime] = None
    caller: Optional[InterviewCaller] = None

    @field_validator("status", mode="before")
    @classmethod
    def _legacy_status(cls, v):
        return normalize_status(v)

    @model_validator(mode="before")
    @classmethod
    def _backfill_id(cls, data):
        if isinstance(data, dict) and not data.get("id"):
            data = dict(data)
            data["id"] = stage_event_id(
                str(data.get("stage") or ""), data.get("at"), data.get("scheduledAt"),
            )
        return data


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
    caller: Optional[InterviewCaller] = None

    # Set when an unmigrated document was resolved on read: questions linked to
    # removed "rejected" rounds must follow on save (routers.interviews._save_interview).
    _question_sync: Optional[dict] = PrivateAttr(default=None)

    @property
    def pending_question_remap(self) -> dict:
        return dict(self._question_sync or {})

    @property
    def needs_question_sync(self) -> bool:
        return self._question_sync is not None

    def clear_question_sync(self) -> None:
        self._question_sync = None

    @model_validator(mode="wrap")
    @classmethod
    def _migrate_legacy_fields(cls, data, handler):
        res = None
        if isinstance(data, dict):
            # Old documents stored interviewer name under `interviewName`.
            if data.get("interviewerName") in (None, "") and data.get("interviewName"):
                data["interviewerName"] = data["interviewName"]
            # Unmigrated documents: rejected stage entries / legacy values.
            res = resolve_interview(data, now=datetime.now(dt_timezone.utc))
            data = res.doc
        iv = handler(data)
        if res is not None and (
            res.question_remap or res.rules.get("rejected_only_to_intro")
        ):
            iv._question_sync = dict(res.question_remap)
        return iv

    class Settings:
        name = "interviews"
        indexes = [
            IndexModel([("scheduledAt", -1)]),
            IndexModel([("createdBy", 1)]),
            IndexModel([("accountId", 1)]),
            IndexModel([("stage", 1)]),
            IndexModel([("status", 1)]),
            IndexModel([("caller.coworkerIds", 1)]),
            IndexModel([("stageHistory.caller.coworkerIds", 1)]),
        ]
