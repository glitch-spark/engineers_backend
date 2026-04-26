from datetime import datetime
from typing import List, Optional

from beanie import PydanticObjectId
from pydantic import BaseModel, Field

from app.models.base import TimestampedDocument


class Resume(BaseModel):
    """Embedded resume document on Account.

    Parsed client-side from PDF/TXT/MD into markdown; backend just stores it.
    Per-resume `id` lets the frontend reference resumes during edits without
    relying on array index (which would break under concurrent edits).
    """
    id: PydanticObjectId = Field(default_factory=PydanticObjectId)
    filename: str
    markdown: str
    uploadedAt: datetime = Field(default_factory=datetime.utcnow)


class Account(TimestampedDocument):
    name: str
    email: str
    phone: Optional[str] = None
    address: Optional[str] = None
    createdBy: PydanticObjectId
    resumes: List[Resume] = []

    class Settings:
        name = "accounts"
