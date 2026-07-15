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

    # Which LLM produced the resume HTML (free NVIDIA tier vs OpenAI fallback).
    resumeLlmProvider: Optional[str] = None  # "free" | "openai"
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

    class Settings:
        name = "resumeapplications"
        indexes = [
            IndexModel([("userId", 1), ("createdAt", -1)], name="user_recent"),
            IndexModel([("accountId", 1), ("createdAt", -1)], name="account_recent"),
        ]
