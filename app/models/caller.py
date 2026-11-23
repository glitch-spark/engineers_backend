"""The callers an interview round can be assigned to (kept by admins and interview managers)."""

from datetime import datetime
from typing import Optional

from pymongo import IndexModel

from app.models.base import TimestampedDocument

# Calendar colours a caller can have (the frontend maps each key to its classes).
CALLER_COLORS = ("sky", "violet", "emerald", "amber", "rose", "teal", "indigo", "orange")
DEFAULT_CALLERS = ("Muhammad", "Pietro", "Jamil's group")


class Caller(TimestampedDocument):
    name: str
    nameKey: str  # lowercased name: one caller per name, whatever the case
    color: str = CALLER_COLORS[0]
    # Set when an admin archives the caller: kept on past rounds, not offered for new ones.
    archivedAt: Optional[datetime] = None
    slackUserId: Optional[str] = None  # member ID in the callers' workspace, for @mentions
    teamId: Optional[str] = None  # CallerTeam id

    class Settings:
        name = "callers"
        indexes = [IndexModel([("nameKey", 1)], unique=True)]
