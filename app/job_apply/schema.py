"""Shapes shared by extraction, gates, scoring, the worker and the API (spec §3.2, §4.1)."""
from datetime import date, datetime
from typing import Literal, Optional

from pydantic import BaseModel, Field

Tier = Literal["required", "core", "mentioned", "preferred", "context"]
WorkMode = Literal["remote", "hybrid", "onsite", "unknown"]


class Term(BaseModel):
    term: str
    aliases: list[str] = Field(default_factory=list)
    tier: Tier = "mentioned"
    soft: bool = False
    # Capitalised name picked up by the rule-based fallback, not from the skills list (weight ×0.5).
    auto: bool = False


class MinYears(BaseModel):
    value: float
    required: bool = True


class Degree(BaseModel):
    level: int = Field(ge=1, le=4)  # 1 associate/diploma, 2 bachelor, 3 master, 4 doctorate
    required: bool = True
    equivalentExperience: bool = False


class Location(BaseModel):
    kind: Literal["country", "region"]
    value: str


class WorkAuth(BaseModel):
    restricted: bool = False
    countries: list[str] = Field(default_factory=list)
    note: Optional[str] = None


class Clearance(BaseModel):
    level: Literal["required", "preferred", "none"] = "none"
    note: Optional[str] = None


class JobExtraction(BaseModel):
    title: str = ""
    company: str = ""
    seniority: Optional[str] = None
    terms: list[Term] = Field(default_factory=list)
    minYears: Optional[MinYears] = None
    degree: Optional[Degree] = None
    workMode: WorkMode = "unknown"
    allowedLocations: list[Location] = Field(default_factory=list)
    # Remote, but only for people living in these cities / metro areas / states ("Austin Metro", "North Carolina").
    remoteAreas: list[str] = Field(default_factory=list)
    workAuthorization: Optional[WorkAuth] = None
    timezoneNote: Optional[str] = None
    clearance: Clearance = Field(default_factory=Clearance)
    postedDate: Optional[date] = None
    # False for error pages, cookie notices, job lists and careers landing pages read in place of a posting.
    isJobPosting: bool = True


class GateResult(BaseModel):
    name: str
    result: Literal["pass", "fail", "unknown"]
    reason: str = ""


class TermHit(BaseModel):
    term: str
    tier: Tier
    weight: float
    credit: float
    match: Literal["exact", "variant", "fuzzy", "missing"]
    where: Literal["recent", "skills", "old", "none"]


class ComponentScore(BaseModel):
    score: float
    weight: int
    detail: str = ""


class ResumeScore(BaseModel):
    accountId: str
    resumeId: str
    filename: str
    total: int
    band: str
    components: dict[str, ComponentScore] = Field(default_factory=dict)
    terms: list[TermHit] = Field(default_factory=list)
    knockouts: list[str] = Field(default_factory=list)
    uploadedAt: Optional[datetime] = None


class TailoredMatch(BaseModel):
    """A tailored resume scored against the job it was written for, the way uploaded resumes are."""
    total: int
    band: str
    knockouts: list[str] = Field(default_factory=list)
    # The job's terms the resume doesn't show, heaviest first.
    missing: list[str] = Field(default_factory=list)
