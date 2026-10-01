"""Unit tests for scoring v2 (spec §3) and the per-row pipeline."""
from datetime import date, datetime

import pytest

from app.job_apply.pipeline import ProfileInfo, ResumeInput, score_row
from app.job_apply.resume_parse import parse_resume
from app.job_apply.schema import Degree, JobExtraction, Location, MinYears, ResumeScore, Term
from app.job_apply.scoring import (
    WEIGHTS,
    WeightedTerm,
    band,
    build_idf,
    combine,
    education_score,
    match_term,
    pick_best,
    relevance,
    score_resume,
    term_weights,
    title_score,
    years_score,
)

RUN = date(2026, 10, 1)


def test_weights_total_100():
    assert WEIGHTS == {"required": 32, "keywords": 27, "years": 13, "title": 11, "education": 9, "relevance": 8}


def test_worked_example_old_weights():
    old = {"required": 30, "keywords": 25, "years": 12, "title": 10, "education": 8, "relevance": 7, "parse": 8}
    parts = {"required": 56, "keywords": 40, "years": 100, "title": 43, "education": None, "relevance": 60, "parse": 80}
    assert combine(parts, old) == 58


def test_combine_renormalises_and_rounds_half_up():
    assert combine({"required": None, "keywords": 50, "years": None, "title": None, "education": None, "relevance": None}) == 50
    assert combine({"keywords": 64.5}, {"keywords": 1}) == 65
    assert combine({}, WEIGHTS) == 0


def test_band():
    assert [band(x) for x in (80, 79, 65, 64, 50, 49)] == ["strong", "good", "good", "fair", "fair", "weak"]


def test_term_weights_adjustments():
    ex = JobExtraction(
        title="Python Developer",
        terms=[
            Term(term="Python", tier="required"),
            Term(term="communication", tier="required", soft=True),
            Term(term="Linear", tier="mentioned", auto=True),
            Term(term="Kafka", tier="preferred"),
        ],
    )
    w = {x.term.term: x.weight for x in term_weights(ex, "Python Python Python. communication. Linear. Kafka x6 Kafka Kafka Kafka Kafka Kafka Kafka")}
    assert w["Python"] == 3.0 + 0.5 + 1.0  # 2 extra mentions, in title
    assert w["communication"] == 1.5  # soft ×0.5
    assert w["Linear"] == 0.75  # auto-detected ×0.5
    assert w["Kafka"] == 1.0 + 1.0  # repetition bonus capped at +1.0


def _wt(term, aliases=()):
    return WeightedTerm(Term(term=term, tier="required", aliases=list(aliases)), 3.0, (term, *aliases))


def test_match_credit_and_evidence():
    r = parse_resume(
        "EXPERIENCE Acme Jan 2023 - Present Postgres tuning. Old Co 2010 - 2015 Perl scripts. SKILLS Kubernetis, Docker",
        RUN,
    )
    m = lambda term, aliases=(): match_term(_wt(term, aliases), r, RUN)  # noqa: E731
    assert m("PostgreSQL", ["Postgres"]).credit == pytest.approx(0.85)  # variant, recent role
    assert m("PostgreSQL", ["Postgres"]).where == "recent"
    assert m("Perl").credit == pytest.approx(0.8) and m("Perl").where == "old"
    assert m("Docker").credit == pytest.approx(0.9) and m("Docker").where == "skills"
    assert m("Kubernetes").credit == pytest.approx(0.7 * 0.9) and m("Kubernetes").match == "fuzzy"
    assert m("Rust").credit == 0 and m("Rust").match == "missing"


def test_short_case_sensitive_terms_do_not_match_words():
    r = parse_resume("EXPERIENCE Acme 2020 - Present Helped teams go faster.", RUN)
    assert match_term(_wt("Go", ["Golang"]), r, RUN).credit == 0


def test_years_score():
    assert years_score(MinYears(value=5, required=True), 3) == pytest.approx(52)
    assert years_score(MinYears(value=5, required=True), 7.9) == 100
    assert years_score(MinYears(value=5, required=True), None) == 50
    assert years_score(MinYears(value=5, required=True), 0) == 0
    assert years_score(None, 3) is None


def test_education_score():
    assert education_score(Degree(level=3, required=True), 2) == 60
    assert education_score(Degree(level=3, required=False), 2) == 75
    assert education_score(Degree(level=3, required=True), 1) == 25
    assert education_score(Degree(level=4, required=True, equivalentExperience=True), 1) == 40
    assert education_score(Degree(level=2, required=False), None) == 20
    assert education_score(Degree(level=2, required=True, equivalentExperience=True), None) == 40
    assert education_score(Degree(level=2, required=True), 3) == 100
    assert education_score(None, 3) is None


def test_title_score_seniority():
    r = parse_resume("Staff Software Engineer EXPERIENCE X Staff Software Engineer 2015 - Present", RUN)
    s, _ = title_score(JobExtraction(title="Junior Software Engineer", seniority="junior"), r)
    assert s == pytest.approx(60)  # core title present (100) × 0.6 (3 levels apart)
    s, _ = title_score(JobExtraction(title="Senior Software Engineer (m/f/d)"), r)
    assert s == pytest.approx(100)  # 1 level apart
    s, _ = title_score(JobExtraction(title="Data Analyst"), r)
    assert s < 60
    assert title_score(JobExtraction(title=""), r)[0] is None


def test_relevance_idf_downweights_boilerplate():
    jds = ["fast-paced team python kafka", "fast-paced team java spring", "fast-paced team react css"]
    idf = build_idf(jds)
    hi, sim = relevance("python kafka pipelines", jds[0], idf)
    lo, _ = relevance("fast-paced team player", jds[0], idf)
    assert hi > lo and 0 < sim <= 1
    assert relevance("", jds[0], idf) == (0.0, 0.0)


def test_pick_best_tiebreak():
    a = ResumeScore(accountId="p", resumeId="a", filename="a", total=80, band="strong", knockouts=["x"])
    b = a.model_copy(update={"resumeId": "b", "knockouts": []})
    assert pick_best([a, b]).resumeId == "b"
    c = b.model_copy(update={"resumeId": "c", "uploadedAt": datetime(2026, 9, 1)})
    d = b.model_copy(update={"resumeId": "d", "uploadedAt": datetime(2026, 9, 20)})
    assert pick_best([c, d]).resumeId == "d"
    assert pick_best([]) is None


JD = "Senior Backend Engineer\nRequirements\n- 5+ years Python and Kafka\n- PostgreSQL\nResponsibilities\n- Build APIs"
EX = JobExtraction(
    title="Senior Backend Engineer",
    seniority="senior",
    terms=[
        Term(term="Python", tier="required"),
        Term(term="Kafka", tier="required"),
        Term(term="PostgreSQL", tier="required", aliases=["Postgres"]),
        Term(term="REST API", tier="core"),
    ],
    minYears=MinYears(value=5, required=True),
    workMode="remote",
    allowedLocations=[Location(kind="country", value="US")],
)
STRONG = "Senior Backend Engineer EXPERIENCE Acme Senior Backend Engineer Jan 2019 - Present Python Kafka Postgres REST APIs."
WEAK = "Designer EXPERIENCE Studio Designer Jan 2024 - Present Figma."


def _ri(acc, rid, text, uploaded=None):
    return ResumeInput(account_id=acc, resume_id=rid, filename=f"{rid}.md", uploaded_at=uploaded, parsed=parse_resume(text, RUN))


def test_score_resume_strong_vs_weak():
    idf = build_idf([JD])
    strong = score_resume(EX, JD, parse_resume(STRONG, RUN), idf, run_date=RUN, account_id="P1", resume_id="s", filename="s.md", uploaded_at=None)
    weak = score_resume(EX, JD, parse_resume(WEAK, RUN), idf, run_date=RUN, account_id="P1", resume_id="w", filename="w.md", uploaded_at=None)
    assert strong.total >= 80 and strong.band == "strong"
    assert weak.total < 50 and "education" not in strong.components
    assert set(strong.components) == {"required", "keywords", "years", "title", "relevance"}
    assert any("Python" in k for k in weak.knockouts) and strong.knockouts == []


def test_score_row_one_per_profile_and_blocking():
    resumes = [_ri("P1", "a", STRONG), _ri("P1", "b", WEAK), _ri("P2", "c", STRONG)]
    profiles = {"P1": ProfileInfo("P1", "One", "US", "US"), "P2": ProfileInfo("P2", "Two", "BR", "Latam")}
    idf = build_idf([JD])
    res = score_row(EX, JD, job_blocked=False, resumes=resumes, profiles=profiles, idf=idf, run_date=RUN)
    assert len(res.scores) == 3
    assert [s.accountId for s in res.best_by_profile] == ["P1"] and res.best_by_profile[0].resumeId == "a"
    assert res.top_score == res.best_by_profile[0].total
    gates = {pg["accountId"]: {g.name: g.result for g in pg["gates"]} for pg in res.profile_gates}
    assert gates["P2"]["location"] == "fail" and gates["P1"]["location"] == "pass"

    blocked = score_row(EX, JD, job_blocked=True, resumes=resumes, profiles=profiles, idf=idf, run_date=RUN)
    assert blocked.best_by_profile == [] and blocked.top_score is None and len(blocked.scores) == 3
