from typing import Optional

from beanie import PydanticObjectId
from pymongo import IndexModel

from app.models.base import TimestampedDocument


class Skill(TimestampedDocument):
    title: str
    minInterviews: int = 1
    maxInterviews: int = 1
    systemPrompt: str = ""
    createdBy: PydanticObjectId

    class Settings:
        name = "skills"
        indexes = [
            IndexModel([("title", 1)], unique=True),
        ]


class AiReviewRun(TimestampedDocument):
    interviewIds: list[PydanticObjectId]
    skillId: Optional[PydanticObjectId] = None
    customPrompt: Optional[str] = None
    output: str = ""
    model: str
    promptTokens: int = 0
    completionTokens: int = 0
    ranBy: PydanticObjectId

    class Settings:
        name = "aireviewruns"
        indexes = [
            IndexModel([("ranBy", 1), ("createdAt", -1)]),
        ]


class GlobalPrompt(TimestampedDocument):
    key: str = "default"
    systemPrompt: str = ""

    class Settings:
        name = "globalprompts"
        indexes = [
            IndexModel([("key", 1)], unique=True),
        ]
