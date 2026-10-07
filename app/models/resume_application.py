from datetime import datetime
from typing import List, Literal, Optional

from beanie import PydanticObjectId
from pydantic import BaseModel
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
    jobUrl: Optional[str] = None

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
    # Logical reading-order text — use instead of copy-pasting from the PDF.
    plainText: Optional[str] = None

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
    jobApplyRowId: Optional[PydanticObjectId] = None
    # Job Applies: this resume scored against that row's job (app/job_apply/tailored_score.py); absent = not scored yet.
    matchScore: Optional[TailoredMatch] = None

    class Settings:
        name = "resumeapplications"
        indexes = [
            IndexModel([("userId", 1), ("createdAt", -1)], name="user_recent"),
            IndexModel([("accountId", 1), ("createdAt", -1)], name="account_recent"),
            IndexModel([("status", 1), ("createdAt", 1)], name="queue"),
            IndexModel([("jobApplyRowId", 1)], name="job_apply_row"),
        ]
