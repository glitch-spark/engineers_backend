"""Which job role a posting is, from its title and extracted skills, and which of a profile's resume prompts fits it.

Keyword based (no LLM call): the title counts most, then required / core skills. The result also carries the
main-stack keywords shown on the job ("marks") and a one-line reason for the suggestion.
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
MAX_MARKS = 7
_TITLE_WEIGHT = 3
_STRONG = ("required", "core")


@dataclass(frozen=True)
class RoleMatch:
    role: Optional[str]
    marks: list[str] = field(default_factory=list)
    reason: str = ""


_KEYWORD_ROLE = {kw: role for role, keywords in ROLE_KEYWORDS.items() for kw in keywords}
# Every keyword as a whole word or phrase, longest first so overlapping ones resolve to the longer ("react native" is
# Mobile, not also "react"); "go" isn't found in "good", but "ci/cd" and "next.js" are.
_PATTERN = re.compile(
    r"(?<![a-z0-9])(?:" + "|".join(re.escape(k) for k in sorted(_KEYWORD_ROLE, key=len, reverse=True)) + r")(?![a-z0-9])"
)


def _find(text: str, keyword: str) -> Optional[re.Match]:
    """`keyword` (any word or phrase) as a whole word in `text`."""
    return re.search(rf"(?<![a-z0-9]){re.escape(keyword)}(?![a-z0-9])", text)


def _keywords_in(text: str) -> list[tuple[int, str]]:
    """(position, keyword) of each role keyword in lowercase `text`, in one pass."""
    return [(m.start(), m.group()) for m in _PATTERN.finditer(text)]


def _weight(term: Term) -> int:
    return 2 if term.tier in _STRONG else 1


def job_role(title: str, terms: Sequence[Term]) -> RoleMatch:
    """The job's role (None without enough signal), up to 7 main-stack marks, and why."""
    low_title = (title or "").lower()
    skills = [s for s in terms if not s.soft]
    in_title: dict[str, int] = {}  # keyword -> first position in the title
    for pos, kw in _keywords_in(low_title):
        in_title.setdefault(kw, pos)
    by_skill: dict[str, Term] = {}  # keyword -> the heaviest skill naming it (keywords in the title are left out)
    for skill in sorted(skills, key=_weight, reverse=True):
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
        scored.append((score, -first, role, title_hits, skill_hits))
    best = max(s[0] for s in scored)
    top = [s for s in scored if s[0] == best]
    if best < 2:
        return RoleMatch(None, _top_skills(skills, [], low_title), "")
    if len(top) > 1:
        top = sorted((s for s in top if s[3]), key=lambda s: s[1], reverse=True)[:1]  # named first in the title
        if not top:
            return RoleMatch(None, _top_skills(skills, [], low_title), "")
    _, _, role, title_hits, skill_hits = top[0]
    role_marks = [s.term for s in sorted(skill_hits, key=_weight, reverse=True)]
    marks = _top_skills(skills, role_marks, low_title)
    parts = []
    if title_hits:
        pos, word = title_hits[0]
        parts.append(f"title says {title[pos:pos + len(word)]}")
    if role_marks:
        parts.append(f"skills {', '.join(role_marks[:2])}")
    return RoleMatch(role, marks, f"{ROLE_LABEL[role]} job — " + "; ".join(parts))


def _top_skills(skills: Sequence[Term], first: list[str], low_title: str) -> list[str]:
    """`first`, then the job's other required / core skills, each once, at most MAX_MARKS; skills the title already
    names are left out (the job row shows the title)."""
    out: list[str] = []
    for name in [*first, *(s.term for s in skills if s.tier in _STRONG)]:
        if name and _find(low_title, name.lower()):
            continue
        if name and name.lower() not in {o.lower() for o in out}:
            out.append(name)
    return out[:MAX_MARKS]


def suggest_prompt(prompts: Sequence, default_id: str, role: Optional[str]) -> str:
    """The prompt for the job's role: the default if it's one of them, else the latest updated; else the default."""
    matching = [p for p in prompts if role and p.role == role]
    if not matching:
        return default_id
    if any(p.id == default_id for p in matching):
        return default_id
    return max(matching, key=lambda p: p.updatedAt).id
