"""Interviews statistics key findings and best / weak group marks (pure)."""

from app.stats import interview_report as ir
from app.stats.interview_findings import FALLBACK, findings, mark_groups
from tests_unit.test_interview_report import NOW, _many

NONE = [{"kind": "none", "text": FALLBACK}]


def _split(passed, decided, **kw):
    return _many(passed, **kw) + _many(decided - passed, "rejected", **kw)


def _run(rows, team):
    rep = ir.report(rows, NOW)
    return rep, findings(rep, team)


def _score(passed, decided, rest_passed, rest_decided, team):
    """The 80+ group against rows of no score; the two together sit at the team rate, so only byScore can fire."""
    return _run(_split(passed, decided, scoreBand="strong") + _split(rest_passed, rest_decided), team)[1]


def test_best_fires_at_one_and_a_half_times_the_team_rate():
    assert _score(60, 200, 20, 200, 20.0) == [{"kind": "best", "text": (
        "80+ passes best for resume score: 30.0% (60 of 200 decided rounds) vs 20.0% overall.")}]
    assert _score(59, 200, 21, 200, 20.0) == NONE


def test_best_needs_the_binomial_evidence():
    assert _score(8, 20, 0, 20, 20.0)[0]["text"].startswith("80+ passes best for resume score: 40.0% (8 of 20 ")
    assert _score(7, 20, 1, 20, 20.0) == NONE  # 35% is over 1.5 x 20%, but P(X >= 7 | 20, 0.2) = 0.087


def test_best_never_a_few_group():
    assert _score(20, 20, 0, 80, 20.0)[0]["kind"] == "best"
    assert _score(19, 19, 0, 76, 20.0) == NONE


def test_weak_fires_at_half_the_team_rate():
    assert _score(40, 200, 120, 200, 40.0) == [{"kind": "weak", "text": (
        "80+: 20.0% pass (40 of 200 decided rounds) vs 40.0% overall.")}]
    assert _score(41, 200, 119, 200, 40.0) == NONE


def test_weak_needs_the_binomial_evidence_and_20_decided():
    assert _score(3, 20, 13, 20, 40.0) == [{"kind": "weak", "text": (
        "80+: 15.0% pass (3 of 20 decided rounds) vs 40.0% overall.")}]
    assert _score(4, 20, 12, 20, 40.0) == NONE  # 20% is half of 40%, but P(X <= 4 | 20, 0.4) = 0.051
    assert _score(0, 19, 16, 21, 40.0) == NONE
    assert _score(0, 20, 16, 20, 40.0)[0]["text"] == "80+: 0.0% pass (0 of 20 decided rounds) vs 40.0% overall."


def test_no_team_rate_no_group_findings():
    assert _score(20, 20, 0, 80, None) == NONE


def test_catch_alls_and_folded_groups_never_named():
    # two few callers pass everything: folded into Other callers; the catch-all rows (No score, Unknown) sit at 0%
    rows = _many(19, caller=("c2", "Dee")) + _many(5, caller=("c3", "Eve")) + _many(96, "rejected")
    rep, found = _run(rows, 20.0)
    assert [f["text"] for f in found] == [
        "Self: 0.0% pass (0 of 96 decided rounds) vs 20.0% overall.",
        "Caller passes best for who took the round: 100.0% (24 of 24 decided rounds) vs 20.0% overall."]
    mark_groups(rep, 20.0)
    other = rep["byCaller"][0]
    assert other["key"] == "other" and other["few"] is False and other["mark"] is None
    assert all(m["mark"] is None for m in other["members"])
    assert [g["mark"] for g in rep["byWho"]] == ["weak", "best"]
    assert all(g["mark"] is None for by in ("byScore", "byArea", "byRegion", "byAppliedBy", "byProfile") for g in rep[by])


def test_top_reason_at_40_percent_of_10_rounds():
    def rejected(top, total, reason="pay"):
        return _run(_many(top, "rejected", reason=reason) + _many(total - top, "rejected"), None)[1]

    assert rejected(4, 10) == [{"kind": "reason", "text": "40.0% of rejected rounds: Pay or contract terms (4 of 10)."}]
    assert rejected(4, 11) == NONE
    assert rejected(4, 9, reason="av") == NONE  # under 10 rounds
    canceled = _many(4, "canceled", reason="company") + _many(6, "canceled")
    assert _run(canceled, None)[1] == [{"kind": "reason", "text": "40.0% of canceled rounds: Company (4 of 10)."}]


def test_top_reason_never_names_unfilled_no_reason_or_other():
    rows = _many(4, "rejected", reason="other") + _many(3, "rejected", reason="no_reason") + _many(3, "rejected")
    assert _run(rows, None)[1] == NONE
    assert _run(_many(10, "rejected", reason="no_reason"), None)[1] == NONE
    assert _run(_many(10, "canceled"), None)[1] == NONE


def test_no_answer_at_30_percent_of_20_decided():
    def run(no_answer, decided):
        return _run(_many(no_answer, "no_answer") + _many(decided - no_answer), None)[1]

    assert run(6, 20) == [{"kind": "no_answer", "text": (
        "30.0% of held rounds got no answer (6 of 20) — follow up after a week.")}]
    assert run(5, 20) == NONE
    assert run(6, 19) == NONE


def test_at_most_four_biggest_effect_first():
    rows = (_many(20, scoreBand="strong", area="Backend", region="EU", by="self")
            + _many(60, "rejected", reason="pay") + _many(20, "no_answer"))
    _, found = _run(rows, 20.0)
    assert [f["kind"] for f in found] == ["best"] * 4  # the reason (75%) and no answer (20%) lines are cut or absent
    assert all(" 100.0% (20 of 20 decided rounds) vs 20.0% overall." in f["text"] for f in found)
    _, found = _run(_many(60, "rejected", reason="pay") + _many(40, "no_answer"), None)
    assert [f["kind"] for f in found] == ["reason", "no_answer"]  # 100% / 40% before 40% / 30%


def test_fallback_when_nothing_fires():
    assert _run([], None)[1] == NONE and _run([], 20.0)[1] == NONE


def test_marks_one_best_and_one_weak_per_table():
    rows = (_split(20, 20, scoreBand="strong") + _split(18, 20, scoreBand="good") + _split(0, 20, scoreBand="fair")
            + _split(1, 20, scoreBand="weak") + _split(8, 20))
    rep, _ = _run(rows, 40.0)
    mark_groups(rep, 40.0)
    assert [(g["key"], g["mark"]) for g in rep["byScore"]] == [
        ("strong", "best"), ("good", None), ("fair", "weak"), ("weak", None), ("unknown", None)]
    for by in ("byStage", "byWho", "byWeekday", "byHour"):
        assert [g["mark"] for g in rep[by] if g["rounds"]] == [None]
