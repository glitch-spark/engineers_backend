from datetime import datetime
from typing import List, Literal, Optional

from beanie import PydanticObjectId
from pydantic import BaseModel, Field

from app.models.base import TimestampedDocument


PageFormat = Literal["A3", "A4", "A5", "Letter"]
# Job roles a resume prompt can be written for (Job Applies suggests the prompt whose role fits the job).
PromptRole = Literal["fullstack", "frontend", "backend", "devops", "data", "ml", "mobile", "qa"]


class ResumePrompt(BaseModel):
    """A resume unit: an HTML template, the prompt written for it, and the job roles it is meant for. Ids are ObjectId
    hex, except "default" for the unit an older profile's template + prompt became (app/resume_prompts.py)."""
    id: str
    name: str
    roles: List[PromptRole] = []
    role: Optional[PromptRole] = None  # before units had several roles; read as [role]
    body: str = ""
    templateHtml: Optional[str] = None  # None = saved before units had templates (inherits the profile template)
    templateName: Optional[str] = None
    pageFormat: PageFormat = "A4"
    updatedAt: datetime = Field(default_factory=datetime.utcnow)


class Resume(BaseModel):
    """Embedded generated-resume record on Account. Parsed client-side from
    PDF/TXT/MD to markdown; backend just stores it. Per-resume `id` lets the
    frontend reference resumes during edits without relying on array index."""
    id: PydanticObjectId = Field(default_factory=PydanticObjectId)
    filename: str
    markdown: str
    uploadedAt: datetime = Field(default_factory=datetime.utcnow)
    # Original uploaded file in S3 (app/resume_files.py); None when S3 isn't configured or for text-only resumes.
    s3Key: Optional[str] = None
    fileSize: Optional[int] = None
    fileType: Optional[str] = None


class Account(TimestampedDocument):
    """A generation profile = (label, HTML template, prompts).

    All candidate data — name, contact line, education, companies, dates,
    projects — lives INSIDE the uploaded HTML template. The resume LLM
    rewrites only the parts the prompt asks for (typically headline, role
    labels, bullets, summary) and preserves the rest byte-for-byte.
    """
    # Profile label, e.g. "AI Full-stack" / "Backend". Identifies the profile
    # in lists, dropdowns, S3 paths, and generated-resume filenames.
    name: str
    # Optional ISO 3166-1 alpha-2 country code (e.g. "US", "BR") for the profile.
    country: Optional[str] = None
    # Optional hiring region: Latam | EU | Canada | US | Asia.
    region: Optional[str] = None
    createdBy: PydanticObjectId
    resumes: List[Resume] = []

    # Account-level custom resume content-contract prompt. Empty → fall back
    # to the user-level prompt, then the built-in default.
    resumePromptBody: Optional[str] = None
    # Several resume prompts, one per role (app/resume_prompts.py). Empty = an older profile: read as one "Default"
    # prompt holding resumePromptBody, which is always kept equal to the default prompt's body.
    resumePrompts: List[ResumePrompt] = []
    defaultPromptId: Optional[str] = None

    # HTML template uploaded by the user. The resume LLM edits this in
    # place per generation — structure/CSS preserved, sample text replaced.
    styleTemplate: Optional[str] = None
    styleTemplateName: Optional[str] = None
    pageFormat: PageFormat = "A4"
    previewHtml: Optional[str] = None

    # Account-level screening-answer prompt. Empty → user-level → built-in.
    screeningPrompt: Optional[str] = None

    # Account-level cover-letter prompt. Empty → fall back to user level.
    coverLetterPrompt: Optional[str] = None

    # When false, hidden from the Resume Generator profile picker.
    showInGenerate: bool = True
    # Archived profiles are hidden from pickers for new work (interviews,
    # resumes) but keep showing on existing records. Absent = active.
    archived: bool = False
    archivedAt: Optional[datetime] = None

    class Settings:
        name = "accounts"
