"""Dedup log for Slack interview bot alerts (the interview day post + the 30-minute round reminders)."""

from datetime import datetime
from typing import Optional

from beanie import PydanticObjectId
from pymongo import IndexModel

from app.models.base import TimestampedDocument


class SlackAlertLog(TimestampedDocument):
    """One row per sent alert. Unique on (userId, kind) prevents cron spam.

    kind examples:
      - interview-day:2026-10-07 (userId = channel sentinel)
      - round-reminder:<roundId>:<startEpoch> (userId = channel sentinel)
    """

    userId: PydanticObjectId
    kind: str
    interviewId: Optional[PydanticObjectId] = None
    sentAt: datetime

    class Settings:
        name = "slackalertlogs"
        indexes = [
            IndexModel([("userId", 1), ("kind", 1)], unique=True),
            IndexModel([("sentAt", 1)]),
        ]
