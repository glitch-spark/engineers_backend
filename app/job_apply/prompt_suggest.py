"""Which job roles a posting is (from its title and extracted skills) and which of a profile's resume units fits it.

Keyword based (no LLM call): the title counts most, then required / core skills. A job can have several roles
(e.g. Full stack + DevOps); the suggested unit is the one whose role marks overlap them most.
"""
import re
from dataclasses import dataclass, field
from typing import Optional, Sequence

from app.job_apply.schema import Term

ROLE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "devops": ("devops", "sre", "site reliability", "platform engineer", "kubernetes", "k8s", "terraform", "ansible",
               "helm", "ci/cd", "jenkins", "infrastructure as code"),
    "frontend": ("frontend", "front-end", "react", "vue", "angular", "next.js", "css", "ui engineer"),
    "fullstack": ("full stack", "fullstack", "full-stack", "mern"),
    "backend": ("backend", "back-end", "microservices", "rest api", "django", "spring", "node.js"),
    "data": ("data engineer", "etl", "spark", "airflow", "dbt", "snowflake"),
    "ml": ("machine learning", "ml engineer", "ai engineer", "llm", "pytorch", "tensorflow", "rag"),
    "mobile": ("ios", "android", "mobile", "swift", "kotlin", "react native", "flutter"),
    "qa": ("qa", "sdet", "test automation", "quality assurance", "selenium", "cypress"),
}
ROLE_LABEL = {
    "fullstack": "Full stack", "frontend": "Frontend", "backend": "Backend", "devops": "DevOps", "data": "Data",
    "ml": "ML / AI", "mobile": "Mobile", "qa": "QA / Test",
}
MAX_ROLES = 3
_TITLE_WEIGHT = 3
_STRONG = ("required", "core")


@dataclass(frozen=True)
class RoleMatch:
    roles: list[str] = field(default_factory=list)  # strongest first
    reason: str = ""


_KEYWORD_ROLE = {kw: role for role, keywords in ROLE_KEYWORDS.items() for kw in keywords}
# Every keyword as a whole word or phrase, longest first so overlapping ones resolve to the longer ("react native" is
# Mobile, not also "react"); "go" isn't found in "good", but "ci/cd" and "next.js" are.
_PATTERN = re.compile(
    r"(?<![a-z0-9])(?:" + "|".join(re.escape(k) for k in sorted(_KEYWORD_ROLE, key=len, reverse=True)) + r")(?![a-z0-9])"
)


def _keywords_in(text: str) -> list[tuple[int, str]]:
    """(position, keyword) of each role keyword in lowercase `text`, in one pass."""
    return [(m.start(), m.group()) for m in _PATTERN.finditer(text)]


def _weight(term: Term) -> int:
    return 2 if term.tier in _STRONG else 1


def job_roles(title: str, terms: Sequence[Term]) -> RoleMatch:
    """The job's roles: each with a score of at least 2 and at least half the top score, at most MAX_ROLES, strongest
    first (ties: first named in the title). Title hits count 3, required / core skills 2, other skills 1."""
    low_title = (title or "").lower()
    in_title: dict[str, int] = {}  # keyword -> first position in the title
    for pos, kw in _keywords_in(low_title):
        in_title.setdefault(kw, pos)
    by_skill: dict[str, Term] = {}  # keyword -> the heaviest skill naming it (keywords in the title are left out)
    for skill in sorted((s for s in terms if not s.soft), key=_weight, reverse=True):
        for name in (skill.term, *skill.aliases):
            for _, kw in _keywords_in(name.lower()):
                if kw not in in_title:
                    by_skill.setdefault(kw, skill)
    scored = []
    for role in ROLE_KEYWORDS:
        title_hits = sorted((pos, kw) for kw, pos in in_title.items() if _KEYWORD_ROLE[kw] == role)
        skill_hits: list[Term] = []
        for kw, skill in by_skill.items():
            if _KEYWORD_ROLE[kw] == role and skill not in skill_hits:
                skill_hits.append(skill)
        score = _TITLE_WEIGHT * len(title_hits) + sum(_weight(s) for s in skill_hits)
        first = title_hits[0][0] if title_hits else len(low_title) + 1
        scored.append((score, first, role, title_hits, skill_hits))
    top = max(s[0] for s in scored)
    if top < 2:
        return RoleMatch()
    picked = sorted((s for s in scored if s[0] >= 2 and 2 * s[0] >= top), key=lambda s: (-s[0], s[1]))[:MAX_ROLES]
    reasons = []
    for _, _, role, title_hits, skill_hits in picked:
        parts = []
        if title_hits:
            pos, word = title_hits[0]
            parts.append(f"title says {title[pos:pos + len(word)]}")
        if skill_hits:
            parts.append("skills " + ", ".join(s.term for s in sorted(skill_hits, key=_weight, reverse=True)[:2]))
        reasons.append(f"{ROLE_LABEL[role]} — " + "; ".join(parts))
    return RoleMatch([s[2] for s in picked], " · ".join(reasons))


def suggest_unit(units: Sequence, default_id: str, roles: Sequence[str]) -> str:
    """The unit whose role marks overlap the job's roles most; ties → one with the job's main (first) role, then the
    default, then the most recently updated. No overlap (or no job roles) → the default."""
    if not roles:
        return default_id
    wanted = set(roles)

    def rank(unit):
        marks = set(unit.roles or [])
        return len(marks & wanted), roles[0] in marks, unit.id == default_id, unit.updatedAt

    best = max(units, key=rank, default=None)
    return best.id if best is not None and rank(best)[0] > 0 else default_id
