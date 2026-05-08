from typing import Optional

from beanie import PydanticObjectId
from pymongo import IndexModel

from app.models.base import TimestampedDocument


class AnalyzeChatLog(TimestampedDocument):
    """Per-turn record of follow-up questions on the cross-interview Analyze
    page. Stateless v1 — chat is not tied to a stored AnalyzeRun, so we save
    the filter signature instead so we can mine question patterns later.
    """

    userId: PydanticObjectId
    accountId: Optional[PydanticObjectId] = None
    filterStages: list[str] = []
    filterFromDate: Optional[str] = None
    filterToDate: Optional[str] = None
    question: str
    answer: str = ""
    model: str = ""
    promptTokens: int = 0
    completionTokens: int = 0

    class Settings:
        name = "analyzechatlogs"
        indexes = [
            IndexModel([("userId", 1), ("createdAt", -1)]),
            IndexModel([("accountId", 1), ("createdAt", -1)]),
        ]
