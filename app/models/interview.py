import hashlib
from datetime import datetime
from datetime import timezone as dt_timezone
from enum import Enum
from typing import Any, List, Literal, Optional
from uuid import uuid4

from beanie import PydanticObjectId
from pydantic import BaseModel, Field, PrivateAttr, field_serializer, field_validator, model_validator
from pymongo import IndexModel

from app.interview_normalize import normalize_status, resolve_interview
from app.job_apply.schema import TailoredMatch
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
    """Optional request for a caller to join this interview."""

    enabled: bool = False
    callerName: str = "TBD"
    # The caller from the caller list (app.models.caller); None = not assigned yet, or a name typed before the list.
    callerId: Optional[str] = None
    # Exact start in UTC; None means the time is still TBD.
    startsAt: Optional[datetime] = None
    timezone: Optional[str] = None  # legacy, no longer written: Slack shows each reader their own time
    method: Optional[CallerMethod] = None
    methodValue: str = ""
    slackChannelTs: Optional[str] = None

    @field_validator("startsAt", mode="after")
    @classmethod
    def _utc_aware(cls, v: Optional[datetime]) -> Optional[datetime]:
        # Mongo returns naive datetimes; tag them UTC so JSON carries the offset.
        if v is not None and v.tzinfo is None:
            return v.replace(tzinfo=dt_timezone.utc)
        return v


def _utc_json(v: Optional[datetime]) -> Optional[str]:
    """ISO with a UTC offset. Mongo hands back naive UTC datetimes, and a browser reads an offset-less string as its own
    local time; the value itself stays naive in Python, where the rest of the code compares it."""
    if v is None:
        return None
    return (v if v.tzinfo else v.replace(tzinfo=dt_timezone.utc)).isoformat().replace("+00:00", "Z")


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
    # Why the round got its status: the recruiter's reason (canceled) or feedback (passed / rejected).
    statusNote: str = ""
    # Per-round details (phase 2). The interview-level interviewerName/caller
    # mirror the latest round's for one release.
    interviewerName: Optional[str] = None
    endsAt: Optional[datetime] = None
    caller: Optional[InterviewCaller] = None
    # A caller round whose caller is lined up (set by the owner or an admin). Cleared when the time changes.
    confirmed: bool = False

    @field_serializer("at", "scheduledAt", "endsAt", when_used="json")
    def _times_utc(self, v: Optional[datetime]) -> Optional[str]:
        return _utc_json(v)

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


# fixed_resume: an uploaded resume picked by hand on the form (kind "uploaded" with this reason). No longer offered:
# fixed-resume forks are found by the company search (kind "fixed"); kept so the links saved with it still load.
NoMatchReason = Literal["fixed_resume", "outside_app", "recruiter", "referral", "name_differs", "other"]


class InterviewResumeLink(BaseModel):
    """The resume and job an interview came from, frozen when linked (for interview statistics). See
    app/interview_resume_link.py and docs/superpowers/specs/2026-10-23-interview-resume-link-design.md."""

    # fixed = a Job Applies fixed-resume fork (app/job_apply/fixed_forks.py); none = no company found (reason says why)
    kind: Literal["tailored", "fixed", "uploaded", "none"]
    linkedBy: Literal["user", "auto"] = "user"  # auto = scripts/backfill_interview_resume_links.py
    linkedAt: datetime = Field(default_factory=datetime.utcnow)
    resumeJobId: Optional[PydanticObjectId] = None  # tailored / fixed: the resume's record (resumeapplications)
    appliedLogId: Optional[PydanticObjectId] = None  # uploaded: the Job Applies applied-log entry
    resumeId: Optional[str] = None  # uploaded: the profile's resume file
    matchedCompany: Optional[str] = None
    source: Optional[Literal["generator", "job_applies"]] = None
    filename: Optional[str] = None
    promptName: Optional[str] = None
    resumeModel: Optional[str] = None
    appliedAt: Optional[datetime] = None  # when the resume was generated / applied with
    jobDescription: Optional[str] = None
    jobUrl: Optional[str] = None
    score: Optional[TailoredMatch] = None  # None = never scored
    reason: Optional[NoMatchReason] = None
    reasonNote: Optional[str] = None

    @field_serializer("linkedAt", "appliedAt", when_used="json")
    def _times_utc(self, v: Optional[datetime]) -> Optional[str]:
        return _utc_json(v)


class InterviewAppliedBy(BaseModel):
    """Who applied for the job: the profile's owner (self) or one of the owner's bidders. Bidders → Performance
    credits a bidder with the interviews tagged to them (app/bids/performance.py)."""

    kind: Literal["self", "bidder"]
    bidderId: Optional[PydanticObjectId] = None  # bidder only
    bidderName: Optional[str] = None  # the bidder's name when saved, so it still reads after a rename or archive


class SlackThread(BaseModel):
    """An interview's #caller thread: its main post."""

    channel: str
    ts: str


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
    confirmed: bool = False  # mirrors the latest round
    # The one #caller thread for all rounds (set by the first caller post).
    slackThread: Optional[SlackThread] = None
    # Which resume and job this interview came from (None = not linked yet: older interviews).
    resumeLink: Optional[InterviewResumeLink] = None
    # Who applied for the job (None = not set: interviews from before the field existed).
    appliedBy: Optional[InterviewAppliedBy] = None

    @field_serializer("scheduledAt", "endsAt", when_used="json")
    def _times_utc(self, v: Optional[datetime]) -> Optional[str]:
        return _utc_json(v)

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
        ]
