"""Job title (from a tailored resume's description or a bid's jobKey) and a keyword-based job area."""

import re
from typing import Optional

_SEP = re.compile(r"\s+[|–—]\s+|\s+-\s+|\s*\|\s*")
_RULES = (
    ("QA", ("qa", "quality", "test", "sdet")),
    ("DevOps", ("devops", "sre", "site reliability", "platform", "infrastructure", "cloud")),
    ("AI / ML", ("ai", "ml", "machine learning", "llm", "genai", "data scien")),
    ("Data", ("data engineer", "analytics", "etl", "bi ")),
    ("Mobile", ("ios", "android", "mobile", "react native", "flutter")),
    ("Full stack", ("full stack", "fullstack", "full-stack")),
    ("Frontend", ("front end", "frontend", "front-end", "react", "angular", "vue", "ui ")),
    ("Backend", ("back end", "backend", "back-end", "python", "java", "golang", "go ", "node", ".net", "ruby", "php",
                 "api")),
)
_RULES_RE = tuple((a, re.compile("|".join(r"(?<![a-z])" + re.escape(k) for k in ks))) for a, ks in _RULES)


def job_title(job_description: Optional[str], job_key: Optional[str]) -> Optional[str]:
    """First line of the description (before its first " | "), else the readable tail of the bid's jobKey, else None."""
    line = next((ln.strip() for ln in (job_description or "").splitlines() if ln.strip()), "")
    line = _SEP.split(line, maxsplit=1)[0].strip()[:120]
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
    t = title.lower() + " "  # trailing space so "ui " / "go " / "bi " match at the end
    return next((a for a, rx in _RULES_RE if rx.search(t)), "Other")
