from beanie import PydanticObjectId
from pymongo import IndexModel

from app.models.base import TimestampedDocument


class InterviewChatLog(TimestampedDocument):
    """Per-turn record of follow-up questions on an interview AI Review.

    Stored separately from the chat session so we can mine question patterns
    later (system prompt tuning, follow-up suggestion generation).
    """

    interviewId: PydanticObjectId
    userId: PydanticObjectId
    question: str
    answer: str = ""
    model: str = ""
    promptTokens: int = 0
    completionTokens: int = 0
    rubric: bool = False

    class Settings:
        name = "interviewchatlogs"
        indexes = [
            IndexModel([("interviewId", 1), ("createdAt", -1)]),
            IndexModel([("userId", 1), ("createdAt", -1)]),
        ]
