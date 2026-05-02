from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from beanie import PydanticObjectId
from pydantic import BaseModel, Field

from app.models.base import TimestampedDocument


StyleMode = Literal["markdown", "structured"]
PageFormat = Literal["A3", "A4", "A5", "Letter"]
ScreeningPromptMode = Literal["markdown", "plaintext"]


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


class CustomLink(BaseModel):
    label: str
    url: str


class Account(TimestampedDocument):
    name: str
    email: str
    phone: Optional[str] = None
    address: Optional[str] = None
    createdBy: PydanticObjectId
    resumes: List[Resume] = []

    label: Optional[str] = None
    education: Optional[str] = None
    experience: Optional[str] = None
    styles: Optional[str] = None

    github: Optional[str] = None
    linkedin: Optional[str] = None
    website: Optional[str] = None
    twitter: Optional[str] = None
    customLinks: List[CustomLink] = []

    styleMode: StyleMode = "markdown"
    styleMarkdown: Optional[str] = None
    styleConfig: Optional[Dict[str, Any]] = None
    pageFormat: PageFormat = "A4"
    previewHtml: Optional[str] = None

    screeningPromptMode: ScreeningPromptMode = "plaintext"
    screeningPrompt: Optional[str] = None

    class Settings:
        name = "accounts"
