"""Unit tests for job extraction: LLM output parsing and the rule-based fallback (no network)."""
import json
import os
from datetime import date

import pytest

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from app.job_apply.extract import build_prompt, extract_job, jd_hash, parse_extraction, rules_extract  # noqa: E402
from app.job_apply.schema import MinYears  # noqa: E402

RUN = date(2026, 10, 1)


def test_parse_fenced_json():
    raw = (
        'Here you go:\n```json\n{"title":"Data Analyst","terms":[{"term":"SQL","tier":"required"}],'
        '"workMode":"remote"}\n```'
    )
    ex = parse_extraction(raw)
    assert ex.title == "Data Analyst" and ex.terms[0].tier == "required" and ex.workMode == "remote"


def test_parse_rejects_garbage():
    with pytest.raises(ValueError):
        parse_extraction("sorry, I can't")
    with pytest.raises(ValueError):
        parse_extraction('["not", "an", "object"]')


def test_parse_tolerates_bad_fields():
    raw = json.dumps({
        "title": "X", "workMode": "Remote", "seniority": "Senior",
        "terms": [{"term": "  "}, {"term": "Go", "tier": "must-have"}],
        "degree": {"level": 9}, "postedDate": "not a date", "clearance": None,
    })
    ex = parse_extraction(raw)
    assert ex.workMode == "remote" and ex.seniority == "senior"
    assert [t.term for t in ex.terms] == ["Go"] and ex.terms[0].tier == "mentioned"
    assert ex.degree is None and ex.postedDate is None and ex.clearance.level == "none"


def test_caps_terms():
    terms = [{"term": f"t{i}", "tier": "required"} for i in range(30)] + [
        {"term": f"m{i}", "tier": "mentioned"} for i in range(40)
    ]
    ex = parse_extraction(json.dumps({"terms": terms}))
    assert sum(t.tier == "required" for t in ex.terms) == 15
    assert len(ex.terms) == 40


def test_rules_extract():
    jd = (
        "Senior Backend Engineer\nRequirements:\n- 5+ years of experience with Python and PostgreSQL\n"
        "- Bachelor's degree or equivalent experience\n- Active Secret clearance required\n"
        "Nice to have:\n- Kubernetes\nThis is a hybrid role, 3 days in office.\nPosted 10 days ago"
    )
    ex = rules_extract(jd, RUN)
    tiers = {t.term: t.tier for t in ex.terms}
    assert tiers["Python"] == "required" and tiers["PostgreSQL"] == "required"
    assert tiers["Kubernetes"] == "preferred"
    assert ex.minYears == MinYears(value=5, required=True)
    assert ex.degree.level == 2 and ex.degree.equivalentExperience
    assert ex.clearance.level == "required" and ex.workMode == "hybrid"
    assert ex.postedDate == date(2026, 9, 21) and ex.seniority == "senior"
    assert ex.title == "Senior Backend Engineer"


def test_rules_remote_and_auto_terms():
    jd = "Fully remote (US only). We page with PagerDuty and track work in Linear.\nRequirements\n- React"
    ex = rules_extract(jd, RUN)
    assert ex.workMode == "remote"
    auto = {t.term for t in ex.terms if t.auto}
    assert "Linear" in auto and "We" not in auto and "US" not in auto
    assert not any(t.auto for t in ex.terms if t.term == "PagerDuty")  # in the skills list


def test_rules_posted_absolute_date():
    assert rules_extract("Date posted: 2026-09-12\nRequirements\n- Python", RUN).postedDate == date(2026, 9, 12)
    assert rules_extract("Posted on September 3, 2026", RUN).postedDate == date(2026, 9, 3)
    assert rules_extract("Posted 30+ days ago", RUN).postedDate == date(2026, 9, 1)
    assert rules_extract("Published: Sept. 3, 2026", RUN).postedDate == date(2026, 9, 3)


def test_build_prompt_contains_rules():
    p = build_prompt("JD TEXT HERE", RUN)
    assert "JD TEXT HERE" in p and "2026-10-01" in p and '"workMode"' in p and "able to obtain" in p


def test_jd_hash_normalises():
    assert jd_hash("Hello  World") == jd_hash("hello world")


async def test_extract_job_uses_llm():
    class R:
        text = '{"title":"T","terms":[{"term":"Python","tier":"required"}]}'

    async def fake(prompt, user, *, validate=None, purpose="llm"):
        validate(R.text)
        return R

    ex, src = await extract_job("Requirements:\n- Python", user=None, run_date=RUN, llm=fake)
    assert src == "llm" and ex.title == "T"


async def test_extract_job_falls_back_on_llm_error():
    async def boom(*a, **k):
        raise RuntimeError("down")

    ex, src = await extract_job("Requirements:\n- Python", user=None, run_date=RUN, llm=boom)
    assert src == "rules" and ex.terms[0].term == "Python"
