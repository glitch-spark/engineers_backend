"""Classified email surfaced to the Pipeline UI.

We persist subject + snippet + from only — never the body. Body is fetched
on demand for the drawer view via the Gmail API, never stored. This keeps
PII surface small and the doc size manageable for inboxes with thousands
of messages.

Each row carries the classifier verdict so the UI can show the proposed
label / target stage even when the auto-advance threshold was not met.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from beanie import PydanticObjectId
from pydantic import BaseModel
from pymongo import IndexModel

from app.models.base import TimestampedDocument


# Labels the classifier may emit. Synced with the classifier prompt — if
# you add one here, update CLASSIFIER_SYSTEM_PROMPT in email_classifier.py.
EmailLabel = Literal[
    "applied",                # confirmation of bid submission
    "recruiter_reachout",     # cold inbound from recruiter
    "phone_screen",
    "pre_screening",
    "take_home",
    "live_coding",
    "system_design",
    "behavioral",
    "panel",
    "final_round",
    "offer",
    "rejection",
    "schedule_interview",     # generic "let's schedule" without a clear stage
    "follow_up",              # user-initiated nudge or recruiter follow-up
    "noise",                  # newsletters, job alerts, OOO replies, etc.
]


# Action taken on receipt of this message — drives the Needs Review tray.
EmailReviewStatus = Literal[
    "on_board",       # actionable → created/advanced a (pending) board card
    "dismissed",      # user rejected the card it fed (No)
    "ignored",        # classifier returned "noise" or "follow_up"
    # legacy values kept so historical rows still validate
    "auto_applied",
    "needs_review",
    "applied",
]


class EmailMessage(TimestampedDocument):
    userId: PydanticObjectId
    accountId: PydanticObjectId
    # Gmail-side identifiers — used to dedupe and to fetch the full body
    # when the drawer is opened.
    messageId: str
    threadId: str
    historyId: Optional[str] = None
    # Header snapshot. snippet is the ~200-char preview Gmail returns;
    # body is NOT persisted.
    fromAddress: str
    fromName: Optional[str] = None
    subject: str = ""
    snippet: str = ""
    receivedAt: datetime
    # Classifier output.
    label: Optional[EmailLabel] = None
    confidence: float = 0.0
    companyGuess: Optional[str] = None
    targetStage: Optional[str] = None  # KanbanStage value, or None
    classifierModel: Optional[str] = None
    # Link to Application if matched.
    applicationId: Optional[PydanticObjectId] = None
    reviewStatus: EmailReviewStatus = "needs_review"
    reviewedAt: Optional[datetime] = None
    reviewedBy: Optional[PydanticObjectId] = None
    # Audit trail — every classification reproducible without re-fetching
    # the body. `evidenceQuote` is the substring the classifier grounded
    # its label on; the sync orchestrator validates it appears in the
    # actual body and downgrades to noise on miss. `sourceLayer` records
    # which decision path produced the verdict so we can attribute
    # accuracy regressions to one layer.
    evidenceQuote: Optional[str] = None
    sourceLayer: Optional[str] = None  # rule_engine | cache | llm | noise_blocklist
    selfDoubt: Optional[float] = None  # model's own uncertainty self-rating (LLM only)
    gmailCategory: Optional[str] = None
    scheduledAt: Optional[datetime] = None
    interviewerName: Optional[str] = None

    class Settings:
        name = "emailMessages"
        indexes = [
            IndexModel([("accountId", 1), ("messageId", 1)], unique=True),
            IndexModel([("userId", 1), ("receivedAt", -1)]),
            IndexModel([("userId", 1), ("reviewStatus", 1), ("receivedAt", -1)]),
            IndexModel([("applicationId", 1)]),
        ]


# ----- API DTOs -----


class EmailMessagePublic(BaseModel):
    id: str
    accountId: str
    messageId: str
    threadId: str
    fromAddress: str
    fromName: Optional[str] = None
    subject: str
    snippet: str
    receivedAt: datetime
    label: Optional[EmailLabel] = None
    confidence: float
    companyGuess: Optional[str] = None
    targetStage: Optional[str] = None
    applicationId: Optional[str] = None
    reviewStatus: EmailReviewStatus
    evidenceQuote: Optional[str] = None
    sourceLayer: Optional[str] = None
    scheduledAt: Optional[datetime] = None
    interviewerName: Optional[str] = None

    @classmethod
    def from_doc(cls, doc: "EmailMessage") -> "EmailMessagePublic":
        return cls(
            id=str(doc.id),
            accountId=str(doc.accountId),
            messageId=doc.messageId,
            threadId=doc.threadId,
            fromAddress=doc.fromAddress,
            fromName=doc.fromName,
            subject=doc.subject,
            snippet=doc.snippet,
            receivedAt=doc.receivedAt,
            label=doc.label,
            confidence=doc.confidence,
            companyGuess=doc.companyGuess,
            targetStage=doc.targetStage,
            applicationId=str(doc.applicationId) if doc.applicationId else None,
            reviewStatus=doc.reviewStatus,
            evidenceQuote=doc.evidenceQuote,
            sourceLayer=doc.sourceLayer,
            scheduledAt=doc.scheduledAt,
            interviewerName=doc.interviewerName,
        )
