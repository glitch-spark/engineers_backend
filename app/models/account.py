from datetime import datetime
from typing import List, Literal, Optional

from beanie import PydanticObjectId
from pydantic import BaseModel, Field

from app.models.base import TimestampedDocument


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


class ContactItem(BaseModel):
    """Generic contact-line entry. User authors a label (e.g. 'Email'),
    display text (the visible value on the resume), an optional URL for
    the href, and a visibility toggle. Label is free-form and editable;
    profiles default-seed a standard 7-row list (Email/Phone/Address/
    GitHub/LinkedIn/Website/Twitter) but users may rename/add/remove."""
    label: str = ""
    display: str = ""
    url: str = ""
    show: bool = True


class Account(TimestampedDocument):
    name: str
    createdBy: PydanticObjectId
    resumes: List[Resume] = []

    education: Optional[str] = None
    experience: Optional[str] = None
    # Free-form notes about projects the candidate has worked on across all
    # companies. LLM attributes each project to a company by reading dates +
    # context. Used to generate the Projects subsection under each company in
    # the resume.
    projects: Optional[str] = None

    # Generic contact items rendered on the resume header. Each entry has
    # display text + url + a visibility toggle. No semantic types — the user
    # writes "mailto:..." / "tel:..." as needed.
    contactItems: List[ContactItem] = []

    # Header title (e.g. "Senior Software Engineer"). When titleAutoFromJD
    # is true, the LLM derives the title from the job description instead;
    # the static text becomes a seniority hint only.
    title: Optional[str] = None
    titleAutoFromJD: bool = False

    # Raw HTML template uploaded by the user. The LLM mimics its
    # structure/CSS/classes and replaces placeholder text with candidate data.
    styleTemplate: Optional[str] = None
    styleTemplateName: Optional[str] = None
    pageFormat: PageFormat = "A4"
    previewHtml: Optional[str] = None

    screeningPromptMode: ScreeningPromptMode = "plaintext"
    screeningPrompt: Optional[str] = None

    class Settings:
        name = "accounts"
