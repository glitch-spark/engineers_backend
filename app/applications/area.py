"""Job title (from a tailored resume's description or a bid's jobKey) and a keyword-based job area."""

import re
from typing import Optional

_WHOLE = frozenset({"java", "ai", "ml", "qa", "sre", "ios", "ui", "go", "bi", "api", "test", "sdet", "etl"})
# first match wins; Data before DevOps ("Data Platform"), weak JS keywords after Backend ("Node.js" is Backend)
_RULES = (
    ("QA", ("qa", "quality", "test", "sdet")),
    ("Data", ("data engineer", "data platform", "data analyst", "data analytics", "analytics", "etl",
              "business intelligence", "bi")),
    ("DevOps", ("devops", "devsecops", "dev ops", "sre", "reliability", "kubernetes", "platform", "infrastructure", "cloud")),
    ("AI / ML", ("ai", "ml", "machine learning", "llm", "genai", "data scien")),
    ("Mobile", ("ios", "android", "mobile", "react native", "flutter")),
    ("Full stack", ("full stack", "fullstack", "full-stack")),
    ("Frontend", ("front end", "frontend", "front-end", "react", "angular", "vue", "ui")),
    ("Backend", ("back end", "backend", "back-end", "python", "java", "golang", "go", "node", ".net", "ruby", "php",
                 "api")),
    ("Frontend", ("javascript", "typescript")),
    # a title naming no speciality ("Senior Software Engineer"): its own group, so Other is the true leftovers
    # (user, 2026-11-20)
    ("General software", ("software engineer", "software developer", "software development", "developer")),
)


def _rx(keywords):
    return re.compile("|".join(
        r"(?<![a-z])" + re.escape(k) + (r"(?![a-z]|/ux)" if k == "ui" else r"(?![a-z])" if k in _WHOLE else "")
        for k in keywords))


_RULES_RE = tuple((a, _rx(ks)) for a, ks in _RULES)


def job_title(job_description: Optional[str], job_key: Optional[str]) -> Optional[str]:
    """First non-empty line of the description (<= 120 chars), else the readable tail of the bid's jobKey, else None."""
    line = next((ln.strip() for ln in (job_description or "").splitlines() if ln.strip()), "")
    line = line[:120]
    if line:
        return line
    tail = (job_key or "").rsplit("/", 1)[-1]
    if "_" not in tail:
        return None
    words = re.split(r"[-_\s]+", tail.split("_", 1)[1])
    if any(re.fullmatch(r"[A-Za-z]{3,}", w) for w in words) and not re.search(r"\d", " ".join(words)):
        return " ".join(words)
    return None


def classify_area(title: Optional[str]) -> Optional[str]:
    if title is None:
        return None
    t = title.lower()
    return next((a for a, rx in _RULES_RE if rx.search(t)), "Other")
