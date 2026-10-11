"""Resume prompt evaluator: the sample job-description library and eval runs.

An eval run freezes the prompt text it used, generates one resume per selected JD, has a judge model
score each, and keeps the per-row facts plus run-level aggregates so runs can be compared later.
"""

from datetime import datetime
from typing import Dict, List, Literal, Optional

from beanie import PydanticObjectId
from pydantic import BaseModel, Field
from pymongo import ASCENDING, DESCENDING, IndexModel

from app.models.base import TimestampedDocument

EvalRole = Literal["fullstack", "frontend", "backend", "ai", "ml", "llm", "data", "test"]
EVAL_ROLES: tuple[EvalRole, ...] = ("fullstack", "frontend", "backend", "ai", "ml", "llm", "data", "test")
ROLE_ORDER: dict[str, int] = {role: index for index, role in enumerate(EVAL_ROLES)}

# Cost guardrail: one run = this many resume generations + judge calls at most.
MAX_JDS_PER_RUN = 12


class EvalJobDescription(TimestampedDocument):
    """A sample posting the evaluator scores prompts against. Shared by all users. Seeds carry a
    `seedKey` and are inserted once; edits and archiving stick."""

    role: EvalRole
    title: str
    company: str
    text: str
    seedKey: Optional[str] = None  # "seed:<role>" for built-ins, None for user-added
    createdBy: Optional[PydanticObjectId] = None
    archived: bool = False
    archivedAt: Optional[datetime] = None

    class Settings:
        name = "eval_job_descriptions"
        indexes = [
            IndexModel([("seedKey", ASCENDING)], unique=True, sparse=True, name="seed_key"),
            IndexModel([("role", ASCENDING), ("archived", ASCENDING)], name="role_archived"),
        ]


EvalRowStatus = Literal["pending", "generating", "judging", "done", "failed"]
EvalRunStatus = Literal["running", "done", "failed"]
PromptSource = Literal["custom", "profile", "global", "none"]


class JudgeVerdict(BaseModel):
    matchScore: int  # 0-100: fit for the posting, location / work authorization ignored
    believabilityScore: int  # 0-100: reads like a real engineer's own resume
    matchNotes: List[str] = []
    believabilityNotes: List[str] = []
    missingRequirements: List[str] = []
    redFlags: List[str] = []
    strengths: List[str] = []
    summary: str = ""


class EvalRow(BaseModel):
    jdId: PydanticObjectId
    role: str
    jdTitle: str
    jdCompany: str
    status: EvalRowStatus = "pending"
    error: Optional[str] = None
    # Resume generation
    genModelId: Optional[str] = None
    genProvider: Optional[str] = None
    genModel: Optional[str] = None
    genFallbackUsed: bool = False
    genCached: bool = False
    genInputTokens: Optional[int] = None
    genOutputTokens: Optional[int] = None
    genCostUsd: Optional[float] = None
    # Judge
    judgeModelId: Optional[str] = None
    judgeProvider: Optional[str] = None
    judgeModel: Optional[str] = None
    judgeFallbackUsed: bool = False
    judgeCached: bool = False
    judgeInputTokens: Optional[int] = None
    judgeOutputTokens: Optional[int] = None
    judgeCostUsd: Optional[float] = None
    # Outputs
    resumeText: Optional[str] = None
    resumeHtml: Optional[str] = None
    verdict: Optional[JudgeVerdict] = None
    durationMs: Optional[int] = None


class EvalCounts(BaseModel):
    total: int = 0
    done: int = 0
    failed: int = 0


class RoleScore(BaseModel):
    match: Optional[float] = None
    believability: Optional[float] = None
    n: int = 0


class ResumePromptEval(TimestampedDocument):
    userId: PydanticObjectId
    accountId: PydanticObjectId
    accountName: str
    label: Optional[str] = None
    promptBody: str = ""  # the custom prompt text actually sent (snapshot)
    promptSource: PromptSource = "custom"
    resumeModelId: Optional[str] = None  # requested ids; None = server default
    judgeModelId: Optional[str] = None
    status: EvalRunStatus = "running"
    error: Optional[str] = None
    startedAt: datetime = Field(default_factory=datetime.utcnow)
    finishedAt: Optional[datetime] = None
    durationMs: Optional[int] = None
    rows: List[EvalRow] = []
    counts: EvalCounts = Field(default_factory=EvalCounts)
    avgMatch: Optional[float] = None
    avgBelievability: Optional[float] = None
    roleScores: Dict[str, RoleScore] = {}
    totalCostUsd: float = 0.0
    cachedRows: int = 0  # rows whose resume came from a cache
    cachedJudgeRows: int = 0  # rows whose verdict came from a cache

    class Settings:
        name = "resume_prompt_evals"
        indexes = [
            IndexModel([("userId", ASCENDING), ("createdAt", DESCENDING)], name="user_recent"),
            IndexModel([("accountId", ASCENDING), ("createdAt", DESCENDING)], name="account_recent"),
            IndexModel([("status", ASCENDING)], name="status"),
        ]
