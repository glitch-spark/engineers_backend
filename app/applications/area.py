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


GENERAL = "General software"

# Technology keywords per area, for a description whose title names no speciality (user, 2026-11-20). Whole words.
_STACK = {
    "Backend": ("python", "django", "flask", "fastapi", "java", "spring", "kotlin", "golang", "go", "node", "node.js",
                "express", "nestjs", ".net", "c#", "ruby", "rails", "php", "laravel", "rust", "scala", "microservices",
                "rest", "graphql", "postgres", "postgresql", "mysql", "sql", "backend", "back-end", "api", "apis"),
    "Frontend": ("react", "angular", "vue", "next.js", "typescript", "javascript", "html", "css", "redux", "tailwind",
                 "frontend", "front-end", "ui", "webpack"),
    "Mobile": ("ios", "android", "swift", "react native", "flutter", "mobile", "xcode"),
    "DevOps": ("kubernetes", "docker", "terraform", "ci/cd", "ansible", "jenkins", "sre", "infrastructure",
               "observability", "helm", "prometheus", "devops", "linux"),
    "Data": ("spark", "airflow", "etl", "snowflake", "dbt", "kafka", "databricks", "data warehouse", "bigquery",
             "data pipeline", "data pipelines", "redshift", "data engineering"),
    "AI / ML": ("machine learning", "llm", "llms", "pytorch", "tensorflow", "ml", "genai", "rag", "nlp",
                "deep learning", "generative ai", "mlops", "langchain"),
    "QA": ("test automation", "selenium", "cypress", "playwright", "qa", "sdet"),
}
_STACK_RE = {a: tuple(re.compile(r"(?<![a-z0-9])" + re.escape(k) + r"(?![a-z0-9])") for k in ks)
             for a, ks in _STACK.items()}
_FULL_STACK_RE = re.compile(r"full[- ]?stack")
_MIN_HITS, _LEAD = 4, 1.5  # an area needs this many distinct keywords and this lead over the runner-up


def area_from_description(text: Optional[str]) -> Optional[str]:
    """The area a job description's technologies point to, or None when no area clearly leads. Counts DISTINCT
    keywords per area (a word repeated ten times counts once). Backend and Frontend both strong, or the text
    saying "full stack", is Full stack."""
    t = (text or "").lower()
    if not t.strip():
        return None
    hits = {a: sum(1 for rx in rxs if rx.search(t)) for a, rxs in _STACK_RE.items()}
    back, front = hits["Backend"], hits["Frontend"]
    if _FULL_STACK_RE.search(t) or (min(back, front) >= _MIN_HITS and min(back, front) * _LEAD >= max(back, front)):
        return "Full stack"
    (top, n1), (_, n2) = sorted(hits.items(), key=lambda x: -x[1])[:2]
    return top if n1 >= _MIN_HITS and n1 >= _LEAD * n2 else None


def classify_area(title: Optional[str]) -> Optional[str]:
    if title is None:
        return None
    t = title.lower()
    return next((a for a, rx in _RULES_RE if rx.search(t)), "Other")
