from datetime import datetime
from typing import Any, Literal, Optional

from beanie import PydanticObjectId
from pymongo import IndexModel
from pydantic import Field

from app.models.base import TimestampedDocument

NotificationKind = Literal["card_renewal", "weekly_plan", "daily_bids"]
NotificationResolution = Literal["none", "paid", "stopped", "opened"]


class Notification(TimestampedDocument):
    """In-app inbox row. Unique on (userId, kind, periodKey, dedupeId) so the
    cron tick is idempotent."""

    userId: PydanticObjectId
    kind: NotificationKind
    title: str
    body: str = ""
    href: str = ""
    unread: bool = True
    periodKey: str
    dedupeId: str
    meta: dict[str, Any] = Field(default_factory=dict)
    ownerUserId: Optional[PydanticObjectId] = None
    # How the recipient handled this row. none = still open.
    resolution: NotificationResolution = "none"
    resolvedAt: Optional[datetime] = None

    class Settings:
        name = "notifications"
        indexes = [
            IndexModel(
                [("userId", 1), ("kind", 1), ("periodKey", 1), ("dedupeId", 1)],
                unique=True,
                name="uniq_notif",
            ),
            IndexModel([("userId", 1), ("unread", 1), ("createdAt", -1)]),
        ]
