from datetime import datetime
from typing import Optional

from beanie import PydanticObjectId
from pydantic import Field
from pymongo import IndexModel

from app.models.base import TimestampedDocument


class InterviewQuestion(TimestampedDocument):
    """One Q&A extracted from an interview transcript.

    Populated by the background extraction worker after an interview's
    transcript is created/updated. The Analyze tab aggregates over these
    rather than re-prompting the LLM with raw transcripts each time.
    """

    interviewId: PydanticObjectId
    userId: PydanticObjectId        # owner — the interview creator
    accountId: Optional[PydanticObjectId] = None
    stage: Optional[str] = None     # snapshot at extraction time
    companyName: Optional[str] = None

    question: str
    candidateAnswer: str = ""

    score: Optional[int] = None              # 0-10
    scoreRationale: Optional[str] = None
    improvementTip: Optional[str] = None

    transcriptVersion: Optional[str] = None  # hash of transcript at extract time
    extractedAt: datetime = Field(default_factory=datetime.utcnow)

    class Settings:
        name = "interviewquestions"
        indexes = [
            IndexModel([("userId", 1), ("stage", 1), ("createdAt", -1)], name="user_stage_recent"),
            IndexModel([("interviewId", 1)], name="by_interview"),
        ]
