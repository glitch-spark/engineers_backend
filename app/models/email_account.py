"""OAuth-connected mailbox owned by a user. Currently gmail-only;
provider field is reserved so outlook can land later without a schema
change.

Refresh tokens are stored encrypted via app.email_crypto. Access tokens
are short-lived (~1h) and refetched on demand — we don't store them.
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Literal, Optional

from beanie import PydanticObjectId
from pydantic import BaseModel
from pymongo import IndexModel

from app.models.base import TimestampedDocument


EmailProvider = Literal["gmail", "outlook"]

EmailSyncStatus = Literal["idle", "running", "error"]


class EmailAccount(TimestampedDocument):
    userId: PydanticObjectId
    provider: EmailProvider = "gmail"
    email: str
    # Fernet-encrypted refresh token. Never logged, never returned via API.
    refreshTokenEncrypted: str
    scopes: List[str] = []
    # Gmail history.list cursor — present once the first full sync completed.
    # First sync runs a date-bounded list; subsequent syncs delta from here.
    historyId: Optional[str] = None
    lastSyncAt: Optional[datetime] = None
    lastSyncError: Optional[str] = None
    syncStatus: EmailSyncStatus = "idle"
    # Disconnected accounts are kept for audit but their refresh token is
    # wiped — set when the user clicks Disconnect or Google revokes us.
    disconnectedAt: Optional[datetime] = None

    class Settings:
        name = "emailAccounts"
        indexes = [
            IndexModel([("userId", 1), ("provider", 1)]),
            IndexModel([("userId", 1), ("email", 1)], unique=True),
        ]


# ----- API DTOs -----


class EmailAccountPublic(BaseModel):
    id: str
    provider: EmailProvider
    email: str
    historyId: Optional[str] = None
    lastSyncAt: Optional[datetime] = None
    lastSyncError: Optional[str] = None
    syncStatus: EmailSyncStatus
    disconnectedAt: Optional[datetime] = None
    createdAt: datetime

    @classmethod
    def from_doc(cls, doc: "EmailAccount") -> "EmailAccountPublic":
        return cls(
            id=str(doc.id),
            provider=doc.provider,
            email=doc.email,
            historyId=doc.historyId,
            lastSyncAt=doc.lastSyncAt,
            lastSyncError=doc.lastSyncError,
            syncStatus=doc.syncStatus,
            disconnectedAt=doc.disconnectedAt,
            createdAt=doc.createdAt,
        )
