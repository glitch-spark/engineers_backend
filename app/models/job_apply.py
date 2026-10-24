"""Job Applies storage.

Runs are short-lived daily worklists (deleted RUN_RETENTION_DAYS after upload). What lasts: job descriptions
cached per URL and LLM readings cached per description (CACHE_DAYS), and the permanent applied log.
"""
from datetime import date, datetime, timedelta
from typing import Dict, List, Literal, Optional

from beanie import PydanticObjectId
from pydantic import BaseModel, Field
from pymongo import ASCENDING, DESCENDING, IndexModel

from app.job_apply.schema import GateResult, JobExtraction, ResumeScore, TailoredMatch
from app.models.base import TimestampedDocument

RunStatus = Literal["queued", "running", "screened", "done", "failed", "cancelled"]
RowStatus = Literal["pending", "fetched", "extracted", "scored", "excluded", "fetch_failed", "llm_failed", "unassigned"]
# A run first screens its jobs (fetch, read, job gates), pauses at "screened" for the user to pick profiles per
# location group, then scores only those job x profile pairs.
RunPhase = Literal["screen", "score"]
ScreenBucket = Literal[
    "valid", "check", "closed", "not_fetched", "read_failed", "not_job", "clearance", "invalid_role", "onsite",
    "too_old", "other_location",
]

# Runs and their rows are deleted by MongoDB this long after upload (TTL index on expiresAt).
RUN_RETENTION_DAYS = 7
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
    selection: List[RunSelection] = Field(default_factory=list)  # set when scoring starts
    phase: RunPhase = "screen"
    # Candidate market (US / UKEU / LATAM, or "none" for no stated location) -> profiles to score its jobs
    # against; "*" applies to every job.
    assignments: Dict[str, List[PydanticObjectId]] = Field(default_factory=dict)
    autoStart: bool = False  # created with profiles up front: score right after screening, no pause
    screenedAt: Optional[datetime] = None
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
    jdSource: Optional[Literal["sheet", "ats_api", "html", "browser", "manual"]] = None
    jdStructured: Optional[bool] = None  # False: read from page text only (no job-site API / JobPosting data)
    status: RowStatus = "pending"
    statusReason: Optional[str] = None
    screen: Optional[ScreenBucket] = None  # set once the row is fetched and read (or fails to be)
    groupKey: Optional[str] = None  # normalised allowed locations, e.g. "GB", "EU+US", "none"
    forceInclude: bool = False  # a person approved this job: it is Worth applying whatever the checks said
    # The resume prompt a person chose per profile (accountId -> prompt id); otherwise the one suggested from the job.
    promptChoice: Dict[str, str] = Field(default_factory=dict)
    markedInvalid: bool = False  # a person moved it out of Worth applying: Invalid role, even over an approval
    title: str = ""
    company: str = ""
    sheetCompany: str = ""  # the company written in the uploaded sheet: kept over fetched, AI and corrected names
    fetchedPostedDate: Optional[date] = None  # from JSON-LD / ATS API; beats the LLM's date
    postedDate: Optional[date] = None
    extraction: Optional[JobExtraction] = None
    extractionSource: Optional[Literal["llm", "rules", "cache", "human"]] = None
    # editedAt of the JobInfo correction this row was read from: a newer one means the row is out of date.
    infoAt: Optional[datetime] = None
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
    # Where that row went, per sheet (accountId → sheetId → {tab, row}): the same job can go to several sheets, and a
    # tailored resume finished later fills its link into each. See app/job_apply/sheet_links.py.
    exportedSheets: Dict[str, Dict[str, dict]] = Field(default_factory=dict)
    # Exports before exportedSheets: accountId → {sheetId, tab, row} (one sheet). Read, no longer written.
    exportedRows: Dict[str, dict] = Field(default_factory=dict)
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
    structured: bool = True  # see FetchResult.structured
    fetcher: int = 0  # fetch.FETCHER_VERSION that read it; pages from an older fetcher are fetched again

    class Settings:
        name = "job_posting_cache"
        indexes = [
            IndexModel([("url", ASCENDING)], unique=True, name="url"),
            # Replaces the old 7-day "ttl" index (dropped at startup, see app/db.py STALE_INDEXES).
            IndexModel([("fetchedAt", ASCENDING)], expireAfterSeconds=CACHE_TTL_SECONDS, name="ttl_30d"),
        ]


class JobInfo(TimestampedDocument):
    """A person's correction of one job, per normalised URL and shared by everyone who uses the link. It beats the
    fetch and the LLM reading in every run (no fetch, no LLM call), never expires, and is never overwritten by a
    machine reading. Written only by the info endpoint (app/routers/job_applies.py)."""

    url: str
    jdText: str  # always set: a correction without a description could not be read or scored
    postedDate: Optional[date] = None
    title: str = ""
    company: str = ""
    extraction: JobExtraction
    editedBy: Optional[PydanticObjectId] = None
    editedByName: str = ""
    editedAt: datetime = Field(default_factory=datetime.utcnow)

    class Settings:
        name = "job_info"
        indexes = [IndexModel([("url", ASCENDING)], unique=True, name="url")]


class JobApproval(TimestampedDocument):
    """A person approved this job link into Worth applying, for everyone who uses the link: other runs pick it up
    when they read the job or, if already read, when opened (`sync_corrections`). Undoing it removes this record."""

    url: str
    approvedBy: Optional[PydanticObjectId] = None
    approvedByName: str = ""
    approvedAt: datetime = Field(default_factory=datetime.utcnow)

    class Settings:
        name = "job_approval"
        indexes = [IndexModel([("url", ASCENDING)], unique=True, name="url")]


class JobInvalidRole(TimestampedDocument):
    """A person moved this job link from Worth applying to Invalid role, for everyone who uses the link: other runs
    pick it up like an approval (`JobApproval`). Undoing it removes this record."""

    url: str
    markedBy: Optional[PydanticObjectId] = None
    markedByName: str = ""
    markedAt: datetime = Field(default_factory=datetime.utcnow)

    class Settings:
        name = "job_invalid_role"
        indexes = [IndexModel([("url", ASCENDING)], unique=True, name="url")]


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
    # Uploaded resumes: the job description and the resume's score with it, kept past the run (and the JD cache) so
    # an interview can be linked to this application later (app/interview_resume_link.py). None on older entries.
    jdText: Optional[str] = None
    score: Optional[TailoredMatch] = None

    class Settings:
        name = "job_application_log"
        indexes = [
            # Replaces "user_url_resume" (dropped at startup, app/db.py STALE_INDEXES; fileKey back-filled first).
            IndexModel([("userId", ASCENDING), ("url", ASCENDING), ("fileKey", ASCENDING)], unique=True, name="user_url_file"),
            IndexModel([("userId", ASCENDING), ("appliedAt", DESCENDING)], name="user_recent"),
            IndexModel([("runId", ASCENDING)], name="run"),
        ]
