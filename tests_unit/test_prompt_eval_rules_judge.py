"""Rules judge: match parts from the Job Applies score, believability checks, verdict notes, cached JD reading."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from datetime import date
from types import SimpleNamespace

import pytest
from beanie import PydanticObjectId

from app.job_apply.resume_parse import parse_resume
from app.job_apply.schema import JobExtraction, Term
from app.llm_router import LlmResult
from app.prompt_eval import rules_judge

RUN = date(2026, 10, 1)

JD = (
    "Senior Backend Engineer. We need Python, PostgreSQL and Kubernetes experience. "
    "Kafka is a plus. You will build reliable services for our payments platform."
)
EX = JobExtraction(
    title="Senior Backend Engineer",
    terms=[
        Term(term="Python", tier="required"),
        Term(term="PostgreSQL", aliases=["Postgres"], tier="required"),
        Term(term="Kubernetes", tier="required"),
        Term(term="Kafka", tier="preferred"),
    ],
)

GOOD = (
    "Jane Doe Senior Backend Engineer\n"
    "EXPERIENCE\n"
    "Senior Backend Engineer, Acme Jan 2022 - Present\n"
    "Built Python services on PostgreSQL and ran them on Kubernetes.\n"
    "Backend Engineer, Beta Jan 2018 - Dec 2021\n"
    "Wrote Python APIs and Kafka consumers.\n"
    "SKILLS\nPython, PostgreSQL, Kubernetes, Kafka\n"
)


def judge(text, jd=JD, ex=EX):
    return rules_judge.judge_rules(
        resume_text=text, jd_text=jd, extraction=ex, idf=rules_judge.build_run_idf([jd]), run_date=RUN,
    )


def part(parts, key):
    return next(p for p in parts if p.key == key)


def test_verdict_has_match_parts_notes_and_a_summary():
    v = judge(GOOD)
    assert 0 <= v.matchScore <= 100 and 0 <= v.believabilityScore <= 100
    assert {p.key for p in v.matchParts} >= {"required", "keywords", "title"}
    assert sum(p.weight for p in v.believabilityParts) == 100
    assert len(v.matchNotes) == len(v.matchParts) and "(weight" in v.matchNotes[0]
    assert len(v.believabilityNotes) == len(v.believabilityParts)
    assert v.summary.startswith(f"Match {v.matchScore}")
    assert any(s.startswith("Python") for s in v.strengths)


def test_missing_terms_are_listed_with_their_tier():
    v = judge(GOOD.replace("Kubernetes", "servers"))
    assert "Kubernetes (required)" in v.missingRequirements
    assert v.matchScore < judge(GOOD).matchScore


def test_skills_only_in_the_skills_list_are_unbacked():
    text = (
        "EXPERIENCE\nEngineer, Acme Jan 2020 - Present\nBuilt internal tools.\n"
        "SKILLS\nPython, PostgreSQL, Kubernetes, Kafka\n"
    )
    v = judge(text)
    unbacked = part(v.believabilityParts, "unbacked")
    assert unbacked.score == 0 and "appear in no role" in unbacked.detail
    assert v.believabilityScore <= 70
    assert any(f.startswith("Skills backed by roles") for f in v.redFlags)


def test_repeated_terms_lose_points():
    stuffed = GOOD + ("Python " * 10)
    rep = part(judge(stuffed).believabilityParts, "repeated")
    assert rep.score < 100 and "Python ×" in rep.detail
    assert part(judge(GOOD).believabilityParts, "repeated").score == 100


def test_text_copied_from_the_posting_loses_points():
    copied = GOOD + "\n" + JD
    cp = part(judge(copied).believabilityParts, "copied")
    assert cp.score < 100 and "copied word for word" in cp.detail


def test_same_stack_in_every_role():
    roles = "".join(
        f"Engineer, Co{i} Jan {2010 + 3 * i} - Dec {2012 + 3 * i}\nPython, PostgreSQL and Kubernetes work.\n"
        for i in range(3)
    )
    v = judge("EXPERIENCE\n" + roles)
    assert part(v.believabilityParts, "same_stack").score == 0
    assert part(judge(GOOD).believabilityParts, "same_stack").score == 100


def test_timeline_flags_overlaps_and_future_dates():
    parsed = parse_resume(
        "EXPERIENCE\nEngineer, A Jan 2018 - Dec 2022\nPython.\nEngineer, B Jan 2020 - Dec 2027\nPython.\n", RUN,
    )
    penalty, detail = rules_judge._timeline(parsed, RUN)
    assert penalty == 10 and "overlap" in detail and "future" in detail


def test_clean_resume_scores_full_believability_on_copy_and_timeline():
    v = judge(GOOD)
    assert part(v.believabilityParts, "copied").score == 100
    assert part(v.believabilityParts, "timeline").score == 100


# ---------- the cached JD reading ----------


class FakeCollection:
    def __init__(self):
        self.updates = []

    async def update_one(self, flt, upd):
        self.updates.append((flt, upd))


@pytest.fixture
def coll(monkeypatch):
    c = FakeCollection()
    monkeypatch.setattr(rules_judge.EvalJobDescription, "get_motor_collection", classmethod(lambda cls: c))
    return c


def a_jd(**over):
    base = dict(id=PydanticObjectId(), text=JD, extraction=None, extractionKey=None)
    base.update(over)
    return SimpleNamespace(**base)


async def test_reading_is_reused_when_the_key_matches(coll):
    async def never(*a, **k):
        raise AssertionError("should not extract")

    jd = a_jd(extraction=EX, extractionKey=rules_judge.reading_key(JD))
    ex, cost, reused = await rules_judge.jd_reading(jd, None, run_date=RUN, extract=never)
    assert ex is EX and cost == 0.0 and reused is True and coll.updates == []


async def test_model_reading_is_priced_and_stored(coll, monkeypatch):
    import app.llm_router as llm_router

    async def fake_complete(prompt, user, **kw):
        return LlmResult(text="{}", provider="anthropic", model="m", usage={"input": 100, "output": 10},
                         fallback_used=False)

    monkeypatch.setattr(llm_router, "complete_with_fallback", fake_complete)
    monkeypatch.setattr(rules_judge, "spec_for_api_model", lambda model: object())
    monkeypatch.setattr(rules_judge, "usage_cost", lambda spec, usage: 0.002)

    async def fake_extract(text, *, user, run_date, llm):
        await llm("prompt", user, validate=None, purpose="extract")
        return EX, "llm"

    jd = a_jd(extraction=EX, extractionKey="old-key")
    ex, cost, reused = await rules_judge.jd_reading(jd, None, run_date=RUN, extract=fake_extract)
    assert ex is EX and cost == pytest.approx(0.002) and reused is False
    (flt, upd), = coll.updates
    assert flt == {"_id": jd.id} and upd["$set"]["extractionKey"] == rules_judge.reading_key(JD)
    assert jd.extractionKey == rules_judge.reading_key(JD)


async def test_rules_fallback_reading_is_not_stored(coll):
    async def fake_extract(text, *, user, run_date, llm):
        return EX, "rules"

    ex, cost, reused = await rules_judge.jd_reading(a_jd(), None, run_date=RUN, extract=fake_extract)
    assert ex is EX and cost is None and reused is False and coll.updates == []
