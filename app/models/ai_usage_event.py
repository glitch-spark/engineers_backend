from typing import Literal, Optional

from beanie import PydanticObjectId
from pymongo import IndexModel

from app.models.base import TimestampedDocument

UsageSource = Literal["live", "backfill"]


class AiUsageEvent(TimestampedDocument):
    """One billed LLM call: who it was for, which feature, which model, tokens and cost.

    `live` events are written by app/ai_usage.py as calls happen. `backfill` events were rebuilt from
    older job / eval / chat records; `estimated` marks those whose per-step split was never stored."""

    userId: Optional[PydanticObjectId] = None
    feature: str
    provider: str = ""
    model: str = ""  # provider API model name, e.g. "gpt-4o-mini", "claude-sonnet-5-5"
    inputTokens: int = 0
    outputTokens: int = 0
    reasoningTokens: int = 0  # already included in outputTokens; kept for reference
    costUsd: Optional[float] = None  # None when the model has no catalog price
    calls: int = 1  # backfill rows can stand for several API calls (a cover letter is 2-4)
    fallbackUsed: bool = False
    ok: bool = True  # False: billed output that failed validation, so the chain moved on
    refId: Optional[str] = None  # job / eval / interview id the call served
    source: UsageSource = "live"
    estimated: bool = False

    class Settings:
        name = "ai_usage_events"
        indexes = [
            IndexModel([("createdAt", -1)], name="created"),
            IndexModel([("userId", 1), ("createdAt", -1)], name="user_recent"),
            IndexModel([("feature", 1), ("createdAt", -1)], name="feature_recent"),
            IndexModel([("source", 1), ("createdAt", -1)], name="source_recent"),
        ]
