from datetime import datetime
from typing import Any, List, Literal, Optional

from beanie import Insert, PydanticObjectId, before_event
from pydantic import BaseModel, field_validator
from pymongo import IndexModel

from app.job_apply.schema import TailoredMatch
from app.models.base import TimestampedDocument


JobStatus = Literal["queued", "in_progress", "completed", "failed"]
JobStep = Literal[
    "queued",
    "generating_resume",
    "rendering_pdf",
    "uploading",
    "generating_answers",
    "done",
]


# Bid counting: tailored resumes from Job Applies count only once the job is marked applied with them.
# Absent/None (old records, Generator page) counts.
BID_FILTER: dict = {"countsAsBid": {"$ne": False}}


def as_match(value: Any) -> Any:
    """A stored matchScore as {total, band, knockouts, missing}. 151 resumes from an early scoring test (2026-09-28)
    hold a bare 0-100 number: it becomes the total, with its band."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        from app.job_apply.scoring import band

        total = int(round(value))
        return {"total": total, "band": band(total), "knockouts": [], "missing": []}
    return value


def job_url_key(url: Optional[str]) -> Optional[str]:
    """A job link's key for "same job" matching: normalised as Job Applies matches links (tracking params, trailing
    slash and platform variants dropped), then without the scheme and a leading www. ("jobs.acme.com/123")."""
    import re

    from app.job_apply.urls import normalize_url

    raw = (url or "").strip()
    if not raw.lower().startswith(("http://", "https://", "www.")):
        return None
    return re.sub(r"^https?://(www\.)?", "", normalize_url(raw), flags=re.IGNORECASE) or None


class ScreeningPair(BaseModel):
    question: str
    answer: str


class CoverLetterVersion(BaseModel):
    """A cover letter that was replaced by a regeneration, kept so it can be compared."""

    text: str
    provider: str = ""
    model: str = ""
    createdAt: datetime


COVER_LETTER_HISTORY_MAX = 5


class ResumeApplication(TimestampedDocument):
    """Backing record for one resume-generation job — covers the whole lifecycle
    (queued → in_progress → completed/failed). The async worker mutates `status`
    and `step` as it advances. Retains all output (PDF bytes, screening pairs,
    error message) so polling clients can render everything without separate
    fetches."""

    userId: PydanticObjectId
    accountId: Optional[PydanticObjectId] = None
    profileName: str = ""           # snapshot at submit time
    companyName: str
    companyNameLower: str
    jobDescription: str
    # Short posting used only for the resume rewrite. Absent on Generator-page jobs and on rows from before tailorJd.
    tailorJobDescription: Optional[str] = None
    jobUrl: Optional[str] = None
    # jobUrl normalised (app/job_apply/urls.normalize_url), set on insert: resumes for the same job across profiles
    # (Statistics → Resume compare). scripts/backfill_job_url_keys.py fills it for resumes made before it existed.
    jobUrlKey: Optional[str] = None

    status: JobStatus = "queued"
    step: JobStep = "queued"
    startedAt: Optional[datetime] = None
    completedAt: Optional[datetime] = None
    executionMs: Optional[int] = None
    errorMessage: Optional[str] = None

    pdfFilename: Optional[str] = None
    pdfData: Optional[str] = None    # base64-encoded; survives without S3
    s3Key: Optional[str] = None
    s3Url: Optional[str] = None
    # Set when a missing PDF was made again (app/resume_restore.py): regenerated from the stored JD and prompt, or a
    # fixed fork re-copied from the profile's file. The lease keeps two restores of one job from running at once.
    pdfRegeneratedAt: Optional[datetime] = None
    pdfRestoreLeaseAt: Optional[datetime] = None
    # Logical reading-order text — use instead of copy-pasting from the PDF.
    plainText: Optional[str] = None
    # Set when retention moved the heavy text (JD, plainText, cover letter, screening) to this S3 archive.
    textArchivedAt: Optional[datetime] = None
    textArchiveKey: Optional[str] = None

    screeningQuestions: List[str] = []
    screeningPairs: List[ScreeningPair] = []

    # LLM token usage from the resume rewrite (gpt-5* reasoning model).
    inputTokens: Optional[int] = None
    outputTokens: Optional[int] = None
    reasoningTokens: Optional[int] = None

    # Which LLM produced the resume HTML. Older records may hold the retired "free" (NVIDIA) provider.
    resumeLlmProvider: Optional[str] = None  # "anthropic" | "openai"
    resumeLlmModel: Optional[str] = None
    resumeLlmFallbackUsed: Optional[bool] = None
    resumeLlmFallbackReason: Optional[str] = None

    # Last job-linked screening batch (initial or Ask panel).
    screeningLlmProvider: Optional[str] = None
    screeningLlmModel: Optional[str] = None
    screeningLlmFallbackUsed: Optional[bool] = None
    screeningLlmFallbackReason: Optional[str] = None

    # Plain-text cover letter generated alongside the resume (opt-in per
    # submission). Empty when the user didn't request one.
    coverLetterText: Optional[str] = None
    # Which model wrote the cover letter (same shape as the resume/screening fields).
    coverLetterLlmProvider: Optional[str] = None  # "openai" | "anthropic"
    coverLetterLlmModel: Optional[str] = None
    coverLetterLlmFallbackUsed: Optional[bool] = None
    coverLetterLlmFallbackReason: Optional[str] = None
    # Letters replaced by "regenerate with another model", newest first (max COVER_LETTER_HISTORY_MAX).
    coverLetterHistory: List[CoverLetterVersion] = []

    # Models the user picked at submit time (catalog ids; None = server default). Kept so
    # retries and queue restarts reuse the same choice.
    resumeModelId: Optional[str] = None
    coverLetterModelId: Optional[str] = None
    screeningModelId: Optional[str] = None
    coverLetterHook: Optional[str] = None  # optional one-line angle for the cover letter

    # Running total of model spend for this job (None until a priced model has run).
    estimatedCostUsd: Optional[float] = None

    # Whether this job was submitted with generateCoverLetter=True — retained
    # so a failed-job retry can honor the original opt-in.
    generateCoverLetter: bool = False

    # ---- generation queue (app/resume_queue.py) ----
    source: Literal["generator", "job_applies"] = "generator"
    countsAsBid: Optional[bool] = None  # False = Job Applies tailoring not yet applied with (not a bid)
    leaseAt: Optional[datetime] = None  # worker heartbeat while generating
    attempts: int = 0  # times the worker claimed this job; 3 interrupted attempts → failed
    promptOverride: Optional[str] = None  # one-off prompt, kept for retries and restarts
    # Which of the profile's resume prompts promptOverride came from (Job Applies), to show it on the job.
    promptId: Optional[str] = None
    promptName: Optional[str] = None
    templateName: Optional[str] = None  # the unit's HTML template, for display
    jobApplyRowId: Optional[PydanticObjectId] = None
    # Job Applies: a fixed-resume fork, not a generation. The profile's uploaded resume (this id) that scoring picked
    # for the job, copied for it (app/job_apply/fixed_forks.py). Not counted toward the daily generation cap.
    fixedFrom: Optional[str] = None
    # Job Applies: this resume scored against that row's job (app/job_apply/tailored_score.py); absent = not scored yet.
    matchScore: Optional[TailoredMatch] = None

    @field_validator("matchScore", mode="before")
    @classmethod
    def _numeric_score(cls, v: Any) -> Any:
        return as_match(v)

    @before_event(Insert)
    def _job_url_key(self) -> None:
        self.jobUrlKey = job_url_key(self.jobUrl)

    class Settings:
        name = "resumeapplications"
        indexes = [
            IndexModel([("jobUrlKey", 1)], name="job_url_key", sparse=True),
            IndexModel([("userId", 1), ("createdAt", -1)], name="user_recent"),
            IndexModel([("accountId", 1), ("createdAt", -1)], name="account_recent"),
            IndexModel([("status", 1), ("createdAt", 1)], name="queue"),
            IndexModel([("jobApplyRowId", 1)], name="job_apply_row"),
        ]
