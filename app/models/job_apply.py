"""Job Applies storage.

Runs are short-lived daily worklists (deleted RUN_RETENTION_DAYS after upload). What lasts: job descriptions
cached per URL and LLM readings cached per description (CACHE_DAYS), and the permanent applied log.
"""
from datetime import date, datetime, timedelta
from typing import Dict, List, Literal, Optional

from beanie import PydanticObjectId
from pydantic import BaseModel, Field
from pymongo import ASCENDING, DESCENDING, IndexModel

from app.job_apply.schema import GateResult, JobExtraction, ResumeScore
from app.models.base import TimestampedDocument

RunStatus = Literal["queued", "running", "done", "failed", "cancelled"]
RowStatus = Literal["pending", "fetched", "extracted", "scored", "excluded", "fetch_failed", "llm_failed"]

# Runs and their rows are deleted by MongoDB this long after upload (TTL index on expiresAt).
RUN_RETENTION_DAYS = 3
# Job description per URL and LLM reading per description. Matches the default max posting age: anything older
# is excluded as stale anyway.
CACHE_DAYS = 30
CACHE_TTL_SECONDS = CACHE_DAYS * 24 * 3600


def _expires_at() -> datetime:
    return datetime.utcnow() + timedelta(days=RUN_RETENTION_DAYS)


class RunSelection(BaseModel):
    accountId: PydanticObjectId
    resumeIds: List[PydanticObjectId] = Field(default_factory=list)  # empty = every resume on the profile


class RunCounts(BaseModel):
    total: int = 0
    fetched: int = 0
    extracted: int = 0
    scored: int = 0
    excluded: int = 0
    failed: int = 0


class JobApplyRun(TimestampedDocument):
    userId: PydanticObjectId
    fileName: str  # uploaded file name, or the Google Sheet's title
    sourceUrl: Optional[str] = None  # Google Sheets link when the run came from one
    status: RunStatus = "queued"
    selection: List[RunSelection]
    threshold: int = 75
    maxAgeDays: int = 30
    # "Today" for the freshness gate and for roles ending "Present"; fixed at upload so a resumed run agrees.
    runDate: datetime = Field(default_factory=datetime.utcnow)
    counts: RunCounts = Field(default_factory=RunCounts)
    heartbeatAt: Optional[datetime] = None  # worker lease; stale after 2 min
    finishedAt: Optional[datetime] = None
    error: Optional[str] = None
    notes: List[str] = Field(default_factory=list)
    expiresAt: datetime = Field(default_factory=_expires_at)

    class Settings:
        name = "job_apply_runs"
        indexes = [
            IndexModel([("userId", ASCENDING), ("createdAt", DESCENDING)], name="user_recent"),
            IndexModel([("status", ASCENDING)], name="status"),
            IndexModel([("expiresAt", ASCENDING)], expireAfterSeconds=0, name="ttl"),
        ]


class ProfileGates(BaseModel):
    accountId: str
    gates: List[GateResult] = Field(default_factory=list)


class AppliedMark(BaseModel):
    """One file the user applied with for this job: an uploaded resume (resumeId) or the job's tailored resume
    (tailoredJobId, a ResumeApplication id). Exactly one is set."""

    accountId: str
    resumeId: Optional[str] = None
    tailoredJobId: Optional[str] = None
    at: datetime = Field(default_factory=datetime.utcnow)


def file_key(resume_id: Optional[str] = None, tailored_job_id: Optional[str] = None) -> str:
    """Identity of the file applied with, as stored in the permanent applied log."""
    return f"tailored:{tailored_job_id}" if tailored_job_id else str(resume_id)


class JobApplyRow(TimestampedDocument):
    runId: PydanticObjectId
    userId: PydanticObjectId
    rowIndex: int
    url: Optional[str] = None
    jdText: Optional[str] = None
    jdSource: Optional[Literal["sheet", "ats_api", "html", "browser"]] = None
    status: RowStatus = "pending"
    statusReason: Optional[str] = None
    title: str = ""
    company: str = ""
    fetchedPostedDate: Optional[date] = None  # from JSON-LD / ATS API; beats the LLM's date
    postedDate: Optional[date] = None
    extraction: Optional[JobExtraction] = None
    extractionSource: Optional[Literal["llm", "rules", "cache"]] = None
    gates: List[GateResult] = Field(default_factory=list)
    scores: List[ResumeScore] = Field(default_factory=list)
    profileGates: List[ProfileGates] = Field(default_factory=list)
    bestByProfile: List[ResumeScore] = Field(default_factory=list)
    topScore: Optional[int] = None
    # Profiles without uploaded resumes this job is open to; you apply with a tailored resume.
    tailorOnly: List[str] = Field(default_factory=list)
    scoredAt: Optional[datetime] = None
    applied: bool = False  # legacy single tick (before per-resume marks); read as "top suggestion applied"
    appliedResumes: List[AppliedMark] = Field(default_factory=list)
    # Tailored resumes for this job, one per profile: accountId → ResumeApplication id (generation queue).
    tailored: Dict[str, PydanticObjectId] = Field(default_factory=dict)
    # Profiles whose row for this job was exported to the Google Sheet: accountId → when.
    exported: Dict[str, datetime] = Field(default_factory=dict)
    expiresAt: datetime = Field(default_factory=_expires_at)

    class Settings:
        name = "job_apply_rows"
        indexes = [
            IndexModel([("runId", ASCENDING), ("rowIndex", ASCENDING)], name="run_row"),
            IndexModel([("runId", ASCENDING), ("status", ASCENDING)], name="run_status"),
            IndexModel([("runId", ASCENDING), ("topScore", DESCENDING)], name="run_top"),
            IndexModel([("expiresAt", ASCENDING)], expireAfterSeconds=0, name="ttl"),
        ]


class JobExtractionCache(TimestampedDocument):
    """LLM extraction keyed by sha256(normalised JD) + prompt version; a JD seen before costs no LLM call."""

    key: str
    extraction: JobExtraction

    class Settings:
        name = "job_extraction_cache"
        indexes = [
            IndexModel([("key", ASCENDING)], unique=True, name="key"),
            IndexModel([("createdAt", ASCENDING)], expireAfterSeconds=CACHE_TTL_SECONDS, name="ttl_30d"),
        ]


class JobPostingCache(TimestampedDocument):
    """Job description per normalised URL, from a fetch or from a sheet; expires CACHE_DAYS after it was stored.
    A later sheet with only the link reuses it with no fetch."""

    url: str
    text: str
    source: str
    postedDate: Optional[date] = None
    title: str = ""
    company: str = ""
    fetchedAt: datetime = Field(default_factory=datetime.utcnow)

    class Settings:
        name = "job_posting_cache"
        indexes = [
            IndexModel([("url", ASCENDING)], unique=True, name="url"),
            # Replaces the old 7-day "ttl" index (dropped at startup, see app/db.py STALE_INDEXES).
            IndexModel([("fetchedAt", ASCENDING)], expireAfterSeconds=CACHE_TTL_SECONDS, name="ttl_30d"),
        ]


class JobApplicationLog(TimestampedDocument):
    """Permanent record of each job applied to, per resume. Outlives the run; later runs read it to flag
    'Already applied' by URL."""

    userId: PydanticObjectId
    url: str  # normalised, same form as JobApplyRow.url
    company: str = ""
    title: str = ""
    accountId: str
    profileName: str = ""
    fileKey: str  # file_key(resumeId, tailoredJobId)
    resumeId: Optional[str] = None
    tailoredJobId: Optional[str] = None
    filename: str = ""
    runId: Optional[PydanticObjectId] = None
    rowId: Optional[PydanticObjectId] = None
    appliedAt: datetime = Field(default_factory=datetime.utcnow)

    class Settings:
        name = "job_application_log"
        indexes = [
            # Replaces "user_url_resume" (dropped at startup, app/db.py STALE_INDEXES; fileKey back-filled first).
            IndexModel([("userId", ASCENDING), ("url", ASCENDING), ("fileKey", ASCENDING)], unique=True, name="user_url_file"),
            IndexModel([("userId", ASCENDING), ("appliedAt", DESCENDING)], name="user_recent"),
            IndexModel([("runId", ASCENDING)], name="run"),
        ]
