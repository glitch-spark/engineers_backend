"""Whole-profile eval routing: each JD gets the unit Job Applies would pick, or a hand-picked one."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from datetime import date
from types import SimpleNamespace

from beanie import PydanticObjectId

from app.job_apply.schema import JobExtraction, Term
from app.models.account import ResumePrompt
from app.prompt_eval import routing
from app.prompt_eval.rules_judge import reading_key

RUN = date(2026, 10, 1)


def account():
    return SimpleNamespace(
        styleTemplate="<html/>", styleTemplateName="t", pageFormat="A4", defaultPromptId="gen", resumePromptBody="",
        resumePrompts=[
            ResumePrompt(id="gen", name="General", body="g", templateHtml="<html>g</html>", templateName="g"),
            ResumePrompt(id="be", name="Backend", roles=["backend"], body="b", templateHtml="<html>b</html>",
                         templateName="b"),
            ResumePrompt(id="fe", name="Frontend", roles=["frontend"], body="f", templateHtml="<html>f</html>",
                         templateName="f"),
        ],
    )


def jd(title, text="We build things.", **over):
    base = dict(id=PydanticObjectId(), title=title, text=text, extraction=None, extractionKey=None)
    base.update(over)
    return SimpleNamespace(**base)


def test_title_routes_to_the_matching_unit():
    j = jd("Senior Backend Engineer")
    (r,), units, default_id = routing.route_jds(account(), [j], {}, RUN)
    assert (r.unitId, r.unitName, r.pick) == ("be", "Backend", "auto")
    assert "title says Backend" in r.reason
    assert default_id == "gen" and [u.id for u in units] == ["gen", "be", "fe"]


def test_no_role_words_routes_to_the_default_unit():
    (r,), _, _ = routing.route_jds(account(), [jd("Software Engineer")], {}, RUN)
    assert r.unitId == "gen" and r.pick == "auto" and r.reason == routing.NO_ROLE_REASON


def test_roles_no_unit_covers_fall_back_to_default_and_say_so():
    (r,), _, _ = routing.route_jds(account(), [jd("Android Developer")], {}, RUN)
    assert r.unitId == "gen" and "no unit is marked for these roles" in r.reason


def test_override_wins_and_unknown_override_is_ignored():
    a, b = jd("Senior Backend Engineer"), jd("Frontend Engineer")
    routes, _, _ = routing.route_jds(account(), [a, b], {str(a.id): "fe", str(b.id): "gone"}, RUN)
    assert (routes[0].unitId, routes[0].pick) == ("fe", "manual")
    assert (routes[1].unitId, routes[1].pick) == ("fe", "auto")


def test_current_stored_reading_is_used_instead_of_rules(monkeypatch):
    text = "Engineer wanted."
    ex = JobExtraction(terms=[Term(term="React", tier="required"), Term(term="Vue", tier="required")])
    j = jd("Engineer", text=text, extraction=ex, extractionKey=reading_key(text))

    def never(*a, **k):
        raise AssertionError("rules_extract must not run")

    monkeypatch.setattr(routing, "rules_extract", never)
    (r,), _, _ = routing.route_jds(account(), [j], {}, RUN)
    assert r.unitId == "fe"


def test_stale_reading_falls_back_to_rules(monkeypatch):
    calls = []

    def fake_rules(text, run_date):
        calls.append(text)
        return JobExtraction(terms=[Term(term="Django", tier="required")])

    monkeypatch.setattr(routing, "rules_extract", fake_rules)
    j = jd("Engineer", text="new text", extraction=JobExtraction(), extractionKey="old")
    (r,), _, _ = routing.route_jds(account(), [j], {}, RUN)
    assert calls == ["new text"] and r.unitId == "be"


def test_older_profile_reads_as_one_default_unit():
    legacy = SimpleNamespace(styleTemplate="<html/>", styleTemplateName="t", pageFormat="A4", resumePromptBody="p",
                             resumePrompts=[], defaultPromptId=None)
    (r,), units, default_id = routing.route_jds(legacy, [jd("Backend Engineer")], {}, RUN)
    assert default_id == "default" and r.unitId == "default" and len(units) == 1
