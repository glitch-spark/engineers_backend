from datetime import datetime
from typing import List, Literal, Optional

from beanie import PydanticObjectId
from pydantic import BaseModel
from pymongo import IndexModel

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


class ScreeningPair(BaseModel):
    question: str
    answer: str


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

    class Settings:
        name = "resumeapplications"
        indexes = [
            IndexModel([("userId", 1), ("createdAt", -1)], name="user_recent"),
            IndexModel([("accountId", 1), ("createdAt", -1)], name="account_recent"),
        ]
