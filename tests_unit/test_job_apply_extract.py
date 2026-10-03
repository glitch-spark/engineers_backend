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


def test_rules_posted_ignores_archived_and_closing_dates():
    assert rules_extract("Archived 1 month ago\nRequirements\n- Python", RUN).postedDate is None
    assert rules_extract("Closes in 3 days. Updated 2 days ago.", RUN).postedDate is None
    assert rules_extract("Archived 1 month ago\nPosted 2 months ago", RUN).postedDate == date(2026, 8, 2)


def _llm_says(text):
    class R:
        pass

    R.text = text

    async def fake(prompt, user, *, validate=None, purpose="llm"):
        validate(R.text)
        return R

    return fake


async def test_extract_job_drops_llm_date_of_today_without_evidence():
    # Asked for a date the posting doesn't give, models answer with "today's date" from the prompt.
    llm = _llm_says('{"title":"T","terms":[{"term":"Python"}],"postedDate":"2026-10-01"}')
    ex, _ = await extract_job("Requirements:\n- Python", user=None, run_date=RUN, llm=llm)
    assert ex.postedDate is None


async def test_extract_job_keeps_llm_date_of_today_when_posted_today():
    llm = _llm_says('{"title":"T","terms":[{"term":"Python"}],"postedDate":"2026-10-01"}')
    ex, _ = await extract_job("Posted today\nRequirements:\n- Python", user=None, run_date=RUN, llm=llm)
    assert ex.postedDate == RUN


async def test_extract_job_drops_future_llm_date():
    llm = _llm_says('{"title":"T","terms":[{"term":"Python"}],"postedDate":"2026-12-01"}')
    ex, _ = await extract_job("Requirements:\n- Python", user=None, run_date=RUN, llm=llm)
    assert ex.postedDate is None


def test_build_prompt_location_and_date_rules():
    p = build_prompt("JD", RUN)
    assert "every country and region" in p and "GMT" in p and "Location:" in p
    assert "archived" in p.lower() and "never today's date" in p.lower()


def test_parse_is_job_posting_flag():
    assert parse_extraction('{"title":"T","terms":[{"term":"Python"}]}').isJobPosting is True
    ex = parse_extraction('{"title":"","terms":[],"isJobPosting":false}')
    assert ex.isJobPosting is False


def test_build_prompt_has_location_and_posting_examples():
    p = build_prompt("JD", RUN)
    assert '"isJobPosting"' in p
    assert "USA, Canada, Europe or Australia" in p and "Remote Global" in p and "UTC" in p
    assert "remote option" in p.lower()


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


def test_parse_normalises_string_lists_and_country_names():
    raw = json.dumps({
        "terms": [{"term": "PostgreSQL", "aliases": "Postgres", "tier": "required"}],
        "workAuthorization": {"restricted": True, "countries": "United States"},
        "allowedLocations": [
            {"kind": "country", "value": "USA"},
            {"kind": "country", "value": "United Kingdom"},
            {"kind": "country", "value": "Europe"},
            {"kind": "region", "value": "LATAM"},
        ],
    })
    ex = parse_extraction(raw)
    assert ex.terms[0].aliases == ["Postgres"]
    assert ex.workAuthorization.countries == ["US"]
    assert [(l.kind, l.value) for l in ex.allowedLocations] == [
        ("country", "US"), ("country", "GB"), ("region", "Europe"), ("region", "LATAM"),
    ]


def test_parse_rejects_empty_extraction():
    with pytest.raises(ValueError):
        parse_extraction("{}")
    with pytest.raises(ValueError):
        parse_extraction('{"workMode": "remote"}')


# ---------- work mode: on-site / hybrid only when the posting says so (Seesaw, 2026-10-03) ----------

SEESAW = "Software Engineer, Backend\nCompany: Seesaw\nLocation: USA\n\nBuild APIs in Python.\nBenefits: Home Office Setup Stipend."


def test_build_prompt_says_unstated_work_mode_is_unknown():
    p = build_prompt("JD", RUN)
    assert 'doesn\'t say where the work is done, workMode is "unknown"' in p


@pytest.mark.parametrize("mode", ["onsite", "hybrid"])
async def test_extract_job_drops_an_unsupported_onsite_or_hybrid_guess(mode):
    llm = _llm_says(json.dumps({"title": "T", "terms": [{"term": "Python"}], "workMode": mode}))
    ex, _ = await extract_job(SEESAW, user=None, run_date=RUN, llm=llm)
    assert ex.workMode == "unknown"


@pytest.mark.parametrize("mode, wording", [
    ("onsite", "This is an on-site role in Austin."),
    ("onsite", "You will work from our London office."),
    ("onsite", "This role is not remote."),
    ("hybrid", "Hybrid: 3 days a week in the office."),
    ("hybrid", "You must live within commuting distance of Berlin."),
])
async def test_extract_job_keeps_onsite_or_hybrid_the_posting_states(mode, wording):
    llm = _llm_says(json.dumps({"title": "T", "terms": [{"term": "Python"}], "workMode": mode}))
    ex, _ = await extract_job(f"{SEESAW}\n{wording}", user=None, run_date=RUN, llm=llm)
    assert ex.workMode == mode


async def test_extract_job_keeps_remote_and_unknown():
    for mode in ("remote", "unknown"):
        llm = _llm_says(json.dumps({"title": "T", "terms": [{"term": "Python"}], "workMode": mode}))
        ex, _ = await extract_job(SEESAW, user=None, run_date=RUN, llm=llm)
        assert ex.workMode == mode


def test_prompt_version_bumped_for_the_work_mode_rule():
    from app.job_apply.extract import PROMPT_VERSION

    assert PROMPT_VERSION == "job-extract-v4"
