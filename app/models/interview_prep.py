from beanie import PydanticObjectId
from pymongo import IndexModel

from app.models.base import TimestampedDocument


class InterviewPrompt(TimestampedDocument):
    """User-owned interview prompt — visible to all authenticated users."""

    userId: PydanticObjectId
    title: str
    body: str = ""

    class Settings:
        name = "interviewprompts"
        indexes = [
            IndexModel([("userId", 1), ("updatedAt", -1)]),
        ]


class InterviewTemplateAnswer(TimestampedDocument):
    """User-owned template answer for common interview questions."""

    userId: PydanticObjectId
    title: str
    body: str = ""

    class Settings:
        name = "interviewtemplateanswers"
        indexes = [
            IndexModel([("userId", 1), ("updatedAt", -1)]),
        ]
