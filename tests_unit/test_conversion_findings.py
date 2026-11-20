"""Conversion key findings and best / weak group marks (pure)."""
from datetime import datetime

from bson import ObjectId

from app.stats import conversion
from app.stats.conversion_findings import FALLBACK, findings, mark_groups

AT = datetime(2026, 10, 10, 15)
CUT = datetime(2026, 10, 20)  # AT rows are matured


def _rows(n, iv=0, **kw):
    """n matured rows, the first iv with an interview; `by` defaults to the catch-all so Self stays out of the way."""
    return [{"appliedAt": AT, "interviewId": ObjectId() if i < iv else None, "by": "unknown", **kw} for i in range(n)]


def _run(rows, team, scope="team"):
    rep = conversion.report(rows, CUT, {}, scope != "team", team)
    return rep, findings(rep, team, scope)


def _kinds(found):
    return [(f["kind"], f["evidence"].get("by"), f["evidence"].get("key")) for f in found]


def test_best_fires_at_one_and_a_half_times_team_rate():
    _, found = _run(_rows(200, 3, scoreBand="strong"), 1.0)
    assert found == [{"kind": "best", "text": "80+ converts best for resume score: 1.5% (3 interviews from 200 "
                                              "matured bids) vs 1.0% overall.",
                      "evidence": {"by": "byScore", "key": "strong", "label": "80+", "rate": 1.5, "interviews": 3,
                                   "matured": 200, "teamRate": 1.0}}]


def test_best_not_below_the_edges():
    assert _run(_rows(201, 3, scoreBand="strong"), 1.0)[1] == [{"kind": "none", "text": FALLBACK, "evidence": {}}]
    # 2 interviews: not enough evidence however high the rate (few by expected too)
    assert _run(_rows(60, 2, scoreBand="strong"), 1.0)[1][0]["kind"] == "none"
    # 3 interviews from 29 matured: few by matured
    assert _run(_rows(29, 3, scoreBand="strong"), 1.0)[1][0]["kind"] == "none"


def test_best_one_per_breakdown_highest_rate_and_plain_by_names():
    rows = (_rows(100, 6, scoreBand="strong", board="a.io", area="Backend", profileAgeDays=3)
            + _rows(100, 4, scoreBand="good", board="b.io", area="Data", profileAgeDays=100))
    _, found = _run(rows, 1.0)
    texts = [f["text"] for f in found]
    assert len(found) == 4 and all(f["kind"] == "best" for f in found)
    assert {f["evidence"]["key"] for f in found} == {"strong", "a.io", "Backend", "0-2w"}
    assert any(" for resume score: 6.0%" in t for t in texts) and any(" for job site:" in t for t in texts)
    assert any(" for job area:" in t for t in texts) and any(" for profile age:" in t for t in texts)


def test_catch_all_groups_never_named():
    rows = _rows(100, 6) + _rows(1000, 0, board="zz.io")  # catch-alls: No job link / No score / Unknown best
    rows += [{**r, "board": f"b{i}.io"} for i, r in enumerate(_rows(9))]  # 9 more boards push one into Other sites
    rep, found = _run(rows, 0.5)
    named = {(f["evidence"]["by"], f["evidence"]["key"]) for f in found}
    assert named == {("bySite", "zz.io")}  # the weak big site; no catch-all best or weak
    assert ("bySite", "none") not in named and ("byScore", None) not in named


def test_weak_fires_at_three_expected_and_not_below():
    _, found = _run(_rows(600, 0, board="x.io"), 0.5)
    assert found == [{"kind": "weak", "text": "x.io: 0 interviews from 600 matured bids — the team rate would give "
                                              "about 3.",
                      "evidence": {"by": "bySite", "key": "x.io", "label": "x.io", "rate": 0.0, "interviews": 0,
                                   "matured": 600, "teamRate": 0.5, "expected": 3.0}}]
    assert _run(_rows(599, 0, board="x.io"), 0.5)[1][0]["kind"] == "none"
    assert _run(_rows(600, 1, board="x.io"), 0.5)[1][0]["kind"] == "none"


def test_no_team_rate_no_best_or_weak():
    assert _run(_rows(600, 9, scoreBand="strong") + _rows(600, 0, board="x.io"), None)[1][0]["kind"] == "none"


def test_gap_in_my_scope_only_at_twice_the_bidder_rate():
    rows = _rows(100, 4, by="self") + _rows(100, 2, by="bidder")
    rep, found = _run(rows, 1.0, "me")
    assert found == [{"kind": "gap", "text": "Your bidder bids convert at 2.0% vs your self bids 4.0% (2 of 100 vs "
                                             "4 of 100).",
                      "evidence": {"by": "byWho", "self": {"interviews": 4, "matured": 100, "rate": 4.0},
                                   "bidder": {"interviews": 2, "matured": 100, "rate": 2.0}}}]
    # the gap replaces Self's own "converts best for who applied" line
    assert not any(f["kind"] == "best" for f in found)
    assert _run(rows, 1.0, "team")[1][0]["kind"] == "best"  # team scope: no gap, Self is just the best
    assert _run(_rows(100, 4, by="self") + _rows(100, 3, by="bidder"), 1.0, "me")[1][0]["kind"] != "gap"
    assert _run(_rows(100, 4, by="self") + _rows(29, 0, by="bidder"), 1.0, "me")[1][0]["kind"] != "gap"
    assert _run(_rows(100, 2, by="self") + _rows(100, 0, by="bidder"), 1.0, "me")[1][0]["kind"] != "gap"


def test_sparse_real_shaped_data_gives_no_chance_finding():
    """Team 0.5 per 100 matured bids; groups with 1–2 interviews from a few hundred bids, big catch-alls."""
    rows = (_rows(150, 2, scoreBand="strong", board="lever.co", area="Backend", profileAgeDays=3, by="self")
            + _rows(300, 1, scoreBand="good", board="greenhouse.io", area="Full stack", profileAgeDays=20,
                    by="bidder")
            + _rows(250, 0, scoreBand="fair", board="ashbyhq.com", area="Frontend", profileAgeDays=50, by="bidder")
            + _rows(80, 0, scoreBand="weak", board="workable.com", area="Data", profileAgeDays=100, by="self")
            + _rows(900, 0))  # no score / no link / unknown age / no tracked bid
    for scope in ("team", "me"):
        rep, found = _run(rows, 0.5, scope)
        assert found == [{"kind": "none", "text": FALLBACK, "evidence": {}}]
        mark_groups(rep, found)
        assert not [g for k in ("byWho", "bySite", "byScore", "byArea", "byAge") for g in rep[k] if g["mark"]]


def test_at_most_four_ordered_by_effect():
    rows = (_rows(100, 6, scoreBand="strong")    # 6.0 / 1.0 = 6x
            + _rows(100, 3, board="a.io")           # 3x: the fifth, dropped
            + _rows(100, 5, area="Backend")          # 5x
            + _rows(100, 4, profileAgeDays=3)        # 4x
            + _rows(450, 0, board="b.io"))           # weak, 4.5 expected
    _, found = _run(rows, 1.0)
    assert [(f["kind"], f["evidence"]["key"]) for f in found] == [
        ("best", "strong"), ("best", "Backend"), ("weak", "b.io"), ("best", "0-2w")]


def test_marks_match_findings_and_skip_members():
    rows = (_rows(200, 6, scoreBand="strong", by="self") + _rows(100, 1, by="bidder")
            + _rows(800, 0, board="x.io") + [{**r, "board": f"b{i}.io"} for i, r in enumerate(_rows(9))])
    rep, found = _run(rows, 0.5, "me")
    mark_groups(rep, found)
    marked = {(k, g["key"]): g["mark"] for k in ("byWho", "bySite", "byScore", "byArea", "byAge", "byBidder")
              for g in rep[k] if g["mark"]}
    assert _kinds(found) == [("best", "byScore", "strong"), ("weak", "bySite", "x.io"), ("gap", "byWho", None)]
    # Bidder (1 of 100 at 0.5) is few by expected: the gap line stands, but few rows are never marked
    assert marked == {("byScore", "strong"): "best", ("bySite", "x.io"): "weak", ("byWho", "self"): "best"}
    other = next(g for g in rep["bySite"] if g["key"] == "other")
    assert other["members"] and all("mark" not in m for m in other["members"])


def test_report_groups_start_unmarked():
    rep = conversion.report(_rows(5, scoreBand="strong"), CUT, {}, True)
    assert all(g["mark"] is None for k in ("byWho", "bySite", "byScore", "byArea", "byAge", "byBidder")
               for g in rep[k])
