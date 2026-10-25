"""Resume prompt evaluator: the sample job-description library and eval runs.

An eval run freezes the prompt text it used, generates one resume per selected JD, has a judge model
score each, and keeps the per-row facts plus run-level aggregates so runs can be compared later.
"""

from datetime import datetime
from typing import Dict, List, Literal, Optional

from beanie import PydanticObjectId
from pydantic import BaseModel, Field
from pymongo import ASCENDING, DESCENDING, IndexModel

from app.job_apply.schema import JobExtraction
from app.models.base import TimestampedDocument

EvalRole = Literal["fullstack", "frontend", "backend", "ai", "ml", "llm", "data", "test", "senior_swe", "staff_swe"]
EVAL_ROLES: tuple[EvalRole, ...] = (
    "fullstack", "frontend", "backend", "ai", "ml", "llm", "data", "test", "senior_swe", "staff_swe",
)
ROLE_ORDER: dict[str, int] = {role: index for index, role in enumerate(EVAL_ROLES)}

# Cost guardrail: one run = this many resume generations + judge calls at most (the whole seed library).
MAX_JDS_PER_RUN = 50


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
    # The rules judge's reading of `text`, reused by later runs while `extractionKey` still matches
    # (jd_hash(text):PROMPT_VERSION).
    extraction: Optional[JobExtraction] = None
    extractionKey: Optional[str] = None

    class Settings:
        name = "eval_job_descriptions"
        indexes = [
            # Partial, not sparse: user-added JDs are saved with an explicit `seedKey: null`, which a sparse
            # index still counts, so only one could ever exist. Only string keys (the built-ins) are unique.
            IndexModel(
                [("seedKey", ASCENDING)],
                unique=True,
                partialFilterExpression={"seedKey": {"$type": "string"}},
                name="seed_key_str",
            ),
            IndexModel([("role", ASCENDING), ("archived", ASCENDING)], name="role_archived"),
        ]


EvalRowStatus = Literal["pending", "generating", "judging", "done", "failed"]
EvalRunStatus = Literal["running", "done", "failed", "stopped"]
PromptSource = Literal["custom", "profile", "global", "none"]
# "llm": a judge model reads each resume. "rules": Job Applies scoring plus text checks, no model per row.
JudgeMode = Literal["llm", "rules"]
# "single": one resume unit on every JD. "profile": each JD gets the unit Job Applies would pick (or an override).
PromptMode = Literal["single", "profile"]
UnitPick = Literal["auto", "manual", "single"]


class EvalUnit(BaseModel):
    """A resume unit as a run used it: the prompt text is frozen; the template is read from the profile."""

    id: str
    name: str
    roles: List[str] = []
    body: str = ""
    templateName: str = ""
    edited: bool = False  # the run's prompt differs from the unit's saved one


class ScorePart(BaseModel):
    """One weighted piece of a rules-judge score, shown as the reason for it."""

    key: str
    label: str
    score: float
    weight: int
    detail: str = ""


class JudgeVerdict(BaseModel):
    matchScore: int  # 0-100: fit for the posting, location / work authorization ignored
    believabilityScore: int  # 0-100: reads like a real engineer's own resume
    matchNotes: List[str] = []
    believabilityNotes: List[str] = []
    missingRequirements: List[str] = []
    redFlags: List[str] = []
    strengths: List[str] = []
    summary: str = ""
    # Rules judge only: how each score was built.
    matchParts: List[ScorePart] = []
    believabilityParts: List[ScorePart] = []


class EvalRow(BaseModel):
    jdId: PydanticObjectId
    role: str
    jdTitle: str
    jdCompany: str
    # The resume unit that generated this row (None on runs from before units).
    unitId: Optional[str] = None
    unitName: Optional[str] = None
    unitPick: Optional[UnitPick] = None
    unitReason: Optional[str] = None
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
    judgeMode: JudgeMode = "llm"
    promptMode: PromptMode = "single"
    units: List[EvalUnit] = []
    unitOverrides: Dict[str, str] = {}  # jdId -> unitId picked by hand, so a re-run can restore them
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
    unitScores: Dict[str, RoleScore] = {}  # by unit id
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
