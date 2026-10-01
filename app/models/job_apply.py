"""Job Applies: one run per uploaded sheet, one row per job (spec §4.1)."""
from datetime import date, datetime
from typing import List, Literal, Optional

from beanie import PydanticObjectId
from pydantic import BaseModel, Field
from pymongo import ASCENDING, DESCENDING, IndexModel

from app.job_apply.schema import GateResult, JobExtraction, ResumeScore
from app.models.base import TimestampedDocument

RunStatus = Literal["queued", "running", "done", "failed", "cancelled"]
RowStatus = Literal["pending", "fetched", "extracted", "scored", "excluded", "fetch_failed", "llm_failed"]

POSTING_CACHE_TTL_SECONDS = 7 * 24 * 3600


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

    class Settings:
        name = "job_apply_runs"
        indexes = [
            IndexModel([("userId", ASCENDING), ("createdAt", DESCENDING)], name="user_recent"),
            IndexModel([("status", ASCENDING)], name="status"),
        ]


class ProfileGates(BaseModel):
    accountId: str
    gates: List[GateResult] = Field(default_factory=list)


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
    scoredAt: Optional[datetime] = None
    applied: bool = False

    class Settings:
        name = "job_apply_rows"
        indexes = [
            IndexModel([("runId", ASCENDING), ("rowIndex", ASCENDING)], name="run_row"),
            IndexModel([("runId", ASCENDING), ("status", ASCENDING)], name="run_status"),
            IndexModel([("runId", ASCENDING), ("topScore", DESCENDING)], name="run_top"),
        ]


class JobExtractionCache(TimestampedDocument):
    """LLM extraction keyed by sha256(normalised JD) + prompt version; a JD seen before costs no LLM call."""

    key: str
    extraction: JobExtraction

    class Settings:
        name = "job_extraction_cache"
        indexes = [IndexModel([("key", ASCENDING)], unique=True, name="key")]


class JobPostingCache(TimestampedDocument):
    """Fetched posting text keyed by normalised URL; expires after 7 days."""

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
            IndexModel([("fetchedAt", ASCENDING)], expireAfterSeconds=POSTING_CACHE_TTL_SECONDS, name="ttl"),
        ]
