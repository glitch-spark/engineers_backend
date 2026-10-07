"""Scoring a tailored resume's text against its job, the way uploaded resumes are scored (no DB)."""
import os
from datetime import date

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from app.job_apply.schema import JobExtraction, MinYears, Term  # noqa: E402
from app.job_apply.scoring import build_idf  # noqa: E402
from app.job_apply.tailored_score import tailored_match  # noqa: E402

RUN = date(2026, 10, 1)
JD = (
    "Senior Python Developer. We need 5+ years building APIs with Python, FastAPI and PostgreSQL. "
    "Kafka is a plus. You will design services and mentor engineers."
)
EX = JobExtraction(
    title="Senior Python Developer",
    seniority="senior",
    minYears=MinYears(value=5, required=True),
    terms=[
        Term(term="Python", tier="required"),
        Term(term="FastAPI", tier="required"),
        Term(term="PostgreSQL", tier="core"),
        Term(term="Kafka", tier="preferred"),
    ],
)
# What generate_resume stores: the HTML's text, one block per line.
TAILORED = """Jane Doe
Senior Python Developer
EXPERIENCE
Senior Python Developer
Acme
Jan 2019 – Present
Built FastAPI services on PostgreSQL in Python; mentored engineers.
SKILLS
Python, FastAPI, PostgreSQL
"""


def test_tailored_match_scores_the_text():
    idf = build_idf([JD, "Java developer with Spring"])
    m = tailored_match(EX, JD, TAILORED, idf, run_date=RUN)
    assert 0 < m.total <= 100
    assert m.band in ("strong", "good", "fair", "weak")
    assert m.knockouts == []
    assert m.missing == ["Kafka"]


def test_tailored_match_lists_missing_heaviest_first():
    idf = build_idf([JD])
    m = tailored_match(EX, JD, "Jane Doe\nEXPERIENCE\nDeveloper\nAcme\n2018 – Present\nWrote Python.", idf, run_date=RUN)
    assert m.missing[:2] == ["FastAPI", "PostgreSQL"]
    assert "Missing required: FastAPI" in m.knockouts


def test_better_text_scores_higher():
    idf = build_idf([JD])
    weak = tailored_match(EX, JD, "Jane Doe\nEXPERIENCE\nDeveloper\nAcme\n2024 – Present\nWrote Java.", idf, run_date=RUN)
    strong = tailored_match(EX, JD, TAILORED, idf, run_date=RUN)
    assert strong.total > weak.total
