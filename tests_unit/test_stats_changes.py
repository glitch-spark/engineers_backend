"""What to change: one test per suggestion pattern (fires / does not fire at its edge), ordering, context builders."""

from datetime import datetime, timedelta

from bson import ObjectId

from app.stats import changes

NOW = datetime(2026, 11, 18, 12)
CUTOFF = NOW - timedelta(days=30)


def _s(matured=600, iv=1, interviews=None, tech=0, median=None):
    return {"matured": matured, "maturedInterviews": iv, "interviews": iv if interviews is None else interviews,
            "per100": round(iv / matured * 100, 1) if matured else None, "reach": {"intro": iv, "tech": tech, "final": 0},
            "days": {"first": median, "median": median, "slowest": median} if median is not None else None}


def _ctx(**kw):
    base = {"team": {"per100": 0.5, "medianDays": 5, "avgScore": 70}, "bestSites": ["Ashby", "Lever"],
            "bestBand": "80+", "matured_days": 30, "scores": {"avg": None, "n": 0},
            "split": {"self": {"matured": 0, "interviews": 0, "per100": None},
                      "bidder": {"matured": 0, "interviews": 0, "per100": None}},
            "bidder": None, "deadSites": [], "prev": None, "quietNow": False, "reasons": []}
    return base | kw


def _patterns(summary, grade="watch", **kw):
    return [x["pattern"] for x in changes.suggest({}, summary, grade, _ctx(**kw))]


# 1. dead
def test_dead_fires_with_matured_days():
    out = changes.suggest({}, _s(iv=0), "dead", _ctx(matured_days=42))
    assert out[0]["pattern"] == "dead"
    assert out[0]["text"] == ("Archive it, or rebuild it with a new base resume / prompt and judge it fresh after "
                              "42 days.")


def test_dead_profile_gets_only_the_dead_suggestion():
    """I1: no "wrong places" / low scores / quiet next to "Archive it"."""
    ctx = dict(scores={"avg": 50, "n": 10}, deadSites=[{"label": "Workday", "matured": 600}], quietNow=True,
               reasons=["intros don't reach tech"])
    assert _patterns(_s(iv=0, median=20), "dead", **ctx) == ["dead"]


def test_dead_not_on_other_grades():
    assert "dead" not in _patterns(_s(iv=0), "watch")


# 2. low_scores (expected = 600 * 0.5 / 100 = 3; per100 0.2 < 0.5)
def test_low_scores_fires_below_edge():
    out = changes.suggest({}, _s(iv=1), "watch", _ctx(scores={"avg": 64.9, "n": 10}))
    assert out[0]["pattern"] == "low_scores"
    assert out[0]["text"] == ("Low interview rate and low resume scores (avg 65 vs team 70) — improve the base resume "
                              "or the role's prompt; check it on Prompt tests.")


def test_low_scores_edges():
    assert "low_scores" not in _patterns(_s(iv=1), scores={"avg": 65, "n": 10})       # not < team - 5
    assert "low_scores" not in _patterns(_s(iv=1), scores={"avg": 50, "n": 9})        # < 10 scored rows
    assert "low_scores" not in _patterns(_s(matured=598, iv=1), scores={"avg": 50, "n": 10})  # expected < 3
    assert "low_scores" not in _patterns(_s(iv=3), scores={"avg": 50, "n": 10})       # at team rate


# 3. wrong_sites
def test_wrong_sites_two_sites_and_drop():
    out = changes.suggest({}, _s(iv=1), "watch", _ctx(deadSites=[{"label": "Workday", "matured": 600}]))
    assert out[0]["pattern"] == "wrong_sites"
    assert out[0]["text"] == ("Wrong places — move bids to Ashby and Lever, where the team converts best; "
                              "drop Workday.")


def test_wrong_sites_drop_only_beyond_chance():
    out = changes.suggest({}, _s(iv=1), "watch", _ctx(deadSites=[{"label": "Workday", "matured": 30}]))
    assert "drop" not in out[0]["text"]  # 30 * 0.5 / 100 = 0.15 expected: chance
    assert "drop" not in changes.suggest({}, _s(iv=1), "watch", _ctx(deadSites=[{"label": "Workday", "matured": 598}]))[0]["text"]


def test_wrong_sites_never_drops_a_best_site():
    dead = [{"label": "Ashby", "matured": 900}, {"label": "Workday", "matured": 600}]
    out = changes.suggest({}, _s(iv=1), "watch", _ctx(deadSites=dead))
    assert out[0]["text"].endswith("where the team converts best; drop Workday.")
    out = changes.suggest({}, _s(iv=1), "watch", _ctx(deadSites=dead[:1]))
    assert "drop" not in out[0]["text"]


def test_wrong_sites_one_site_no_drop():
    out = changes.suggest({}, _s(iv=1), "watch", _ctx(bestSites=["Ashby"]))
    assert out[0]["text"] == "Wrong places — move bids to Ashby, where the team converts best."


def test_wrong_sites_good_resumes_only_with_ten_scored_at_or_above_team():
    """I1: "Good resumes" needs >= 10 scored rows averaging at least the team's."""
    good = changes.suggest({}, _s(iv=1), "watch", _ctx(bestSites=["Ashby"], scores={"avg": 70, "n": 10}))
    assert good[0]["text"] == "Good resumes, wrong places — move bids to Ashby, where the team converts best."
    for sc in ({"avg": 90, "n": 9}, {"avg": 69.9, "n": 10}, {"avg": None, "n": 0}):
        out = changes.suggest({}, _s(iv=1), "watch", _ctx(bestSites=["Ashby"], scores=sc))
        assert out[0]["text"].startswith("Wrong places — move bids to Ashby"), sc


def test_wrong_sites_skipped():
    assert "wrong_sites" not in _patterns(_s(iv=1), bestSites=[])
    assert "wrong_sites" not in _patterns(_s(iv=1), scores={"avg": 50, "n": 10})  # low_scores instead
    assert "wrong_sites" not in _patterns(_s(matured=598, iv=1))                  # expected < 3


# 4. stuck_intro
def test_stuck_intro_from_grade_reason():
    out = changes.suggest({}, _s(iv=3), "watch", _ctx(reasons=["intros don't reach tech"]))
    assert [x["pattern"] for x in out] == ["stuck_intro"]
    assert out[0]["text"] == "Intros don't turn into tech rounds — tone the prompt down to real skills and add interview prep."
    assert "stuck_intro" not in _patterns(_s(iv=3), reasons=["no interview has reached tech yet"])


# 5. slow
def test_slow_edges():
    out = changes.suggest({}, _s(iv=3, median=10), "watch", _ctx())
    assert out[0]["text"] == ("Interviews come slowly (10 days vs team 5, from 3 interviews) — sharpen the summary; "
                              "try a winning profile's template.")
    assert "slow" not in _patterns(_s(iv=3, median=9))
    assert "slow" not in _patterns(_s(iv=3, median=10), team={"per100": 0.5, "medianDays": 0, "avgScore": 70})


def test_slow_needs_three_interviews():
    """I3: no advice from one or two data points."""
    assert "slow" not in _patterns(_s(iv=2, median=20))
    assert "slow" not in _patterns(_s(iv=1, interviews=1, median=20))
    assert "slow" in _patterns(_s(iv=1, interviews=3, median=20))


# 6. bidder_gap
def _split(sm, si, bm, bi):
    return {"self": {"matured": sm, "interviews": si, "per100": round(si / sm * 100, 1)},
            "bidder": {"matured": bm, "interviews": bi, "per100": round(bi / bm * 100, 1)}}


def test_bidder_gap_fires():
    out = changes.suggest({}, _s(iv=3), "watch", _ctx(split=_split(40, 4, 60, 3), bidder="Bob"))
    assert out[0]["pattern"] == "bidder_gap"
    assert out[0]["text"] == ("Your own applies convert better than your bidders' (4 of 40 vs 3 of 60 matured bids) — "
                              "review Bob's jobs and rejected share.")


def test_bidder_gap_edges():
    assert "bidder_gap" not in _patterns(_s(iv=3), split=_split(29, 3, 60, 0))   # self matured < 30
    assert "bidder_gap" not in _patterns(_s(iv=3), split=_split(40, 4, 29, 0))   # bidder matured < 30
    assert "bidder_gap" not in _patterns(_s(iv=3), split=_split(40, 4, 50, 3))   # 10.0 vs 6.0: not 2x
    assert "bidder_gap" not in _patterns(_s(iv=3), split=_split(40, 4, 29, 0))   # 29 * 10 / 100 < 3
    assert "bidder_gap" in _patterns(_s(iv=3), split=_split(40, 4, 30, 0))       # 30 * 10 / 100 = 3


def test_bidder_gap_needs_three_self_interviews():
    assert "bidder_gap" not in _patterns(_s(iv=3), split=_split(30, 1, 300, 0))
    assert "bidder_gap" not in _patterns(_s(iv=3), split=_split(30, 2, 300, 0))
    assert "bidder_gap" in _patterns(_s(iv=3), split=_split(30, 3, 300, 0))


def test_bidder_gap_zero_bidder_rate_text():
    out = changes.suggest({}, _s(iv=3), "watch", _ctx(split=_split(40, 4, 30, 0), bidder="Bob"))
    assert out[0]["text"].startswith("Your own applies convert better than your bidders' (4 of 40 vs 0 of 30 matured bids)")
    assert "far" not in out[0]["text"]


# 7. volume_up_rate_down (team 1.0: expected 4 and 5)
def _vol(matured, iv, prev_matured=400, prev_iv=4, band="80+"):
    return changes.suggest({}, _s(matured=matured, iv=iv), "watch",
                           _ctx(team={"per100": 1.0, "medianDays": 5, "avgScore": 70}, bestSites=[], bestBand=band,
                                prev=_s(matured=prev_matured, iv=prev_iv)))


def test_volume_up_rate_down_fires_at_edge():
    out = _vol(800, 7)  # +100% bids; 0.9 vs 1.0 -> -10%: no
    assert "volume_up_rate_down" not in [x["pattern"] for x in out]
    out = _vol(500, 3)  # +25%; 0.6 vs 1.0 -> -40%
    assert out[0]["pattern"] == "volume_up_rate_down"
    assert out[0]["text"] == ("More bids, fewer interviews per bid — fewer, better-matched applies (score 80+ converts "
                              "best for the team).")
    assert _vol(500, 3, band=None)[0]["text"] == "More bids, fewer interviews per bid — fewer, better-matched applies."


def test_volume_up_rate_down_edges():
    assert "volume_up_rate_down" not in [x["pattern"] for x in _vol(499, 3)]                # +24.75%
    assert "volume_up_rate_down" not in [x["pattern"] for x in _vol(500, 3, prev_matured=400, prev_iv=0)]
    assert "volume_up_rate_down" not in [x["pattern"] for x in _vol(500, 3, prev_matured=200, prev_iv=2)]  # prev exp 1


# 8. quiet_now
def test_quiet_now_flag():
    out = changes.suggest({}, _s(matured=10, iv=0), "early", _ctx(quietNow=True))
    assert out == [{"pattern": "quiet_now", "evidence": {"days": 21},
                    "text": "No interview on this profile for 21+ days while it's still bidding — check the base resume "
                            "and the jobs it's applied to."}]


def test_no_negative_suggestion_when_too_few():
    ctx = dict(scores={"avg": 50, "n": 10}, split=_split(40, 4, 60, 0), bidder="Bob",
               prev=_s(matured=40, iv=4), quietNow=True)
    assert _patterns(_s(iv=1, median=20), "early", **ctx) == ["quiet_now"]


def test_max_three_suggestions_in_order():
    out = _patterns(_s(iv=3, median=20), "watch", reasons=["intros don't reach tech"], split=_split(40, 4, 60, 0),
                    quietNow=True, team={"per100": 1.0, "medianDays": 5, "avgScore": 70})
    assert out == ["wrong_sites", "stuck_intro", "slow"]


# context builders
def _row(by="self", board=None, iv=False, score=None, matured=True, days_ago=None, bidder=None, status=None):
    return {"accountId": ObjectId(), "appliedAt": NOW - timedelta(days=days_ago or (40 if matured else 5)), "by": by,
            "board": board, "interviewId": ObjectId() if iv else None, "score": score, "bidderId": bidder,
            "bidStatus": status, "scoreBand": None, "daysToInterview": 3 if iv else None}


def test_profile_context_split_scores_worst_site_and_bidder():
    b1, b2 = ObjectId(), ObjectId()
    rows = ([_row(iv=i < 3) for i in range(30)] + [_row("bidder", "myworkdayjobs.com", bidder=b1) for _ in range(30)]
            + [_row("bidder", "lever.co", bidder=b2, score=60) for _ in range(10)]
            + [_row("bidder", "lever.co", bidder=b2, matured=False) for _ in range(30)]
            + [_row("bidder", "lever.co", status="rejected", score=0) for _ in range(5)])
    ctx = changes.profile_context(rows, CUTOFF)
    assert ctx["split"] == {"self": {"matured": 30, "interviews": 3, "per100": 10.0},
                            "bidder": {"matured": 40, "interviews": 0, "per100": 0.0}}
    assert ctx["scores"] == {"avg": 60.0, "n": 10}
    assert ctx["deadSites"] == [{"label": "Workday", "matured": 30}, {"label": "Lever", "matured": 10}]
    assert ctx["topBidderId"] == b1        # most matured rows (b2 has more rows, fewer matured)


def test_team_context_best_sites_and_band():
    rows = ([_row(board="ashbyhq.com", iv=i < 6, score=85) for i in range(30)]
            + [_row(board="lever.co", iv=i < 3) for i in range(30)]
            + [_row(board="greenhouse.io", iv=i < 9) for i in range(29)]  # too few
            + [_row(board="myworkdayjobs.com") for _ in range(30)])
    for r in rows[:30]:
        r["scoreBand"] = "strong"
    ctx = changes.team_context(rows, CUTOFF, {"per100": 0.5, "medianDays": 5})
    assert ctx["bestSites"] == ["Ashby", "Lever"]
    assert ctx["bestBand"] == "80+"
    assert ctx["team"] == {"per100": 0.5, "medianDays": 5, "avgScore": 85.0}


def test_quiet_candidate():
    old, recent = _row(days_ago=60), _row(days_ago=2)
    assert changes.quiet_candidate([old, recent], NOW, 30)
    assert not changes.quiet_candidate([_row(days_ago=50), recent], NOW, 30)        # first row 50 < 51 days
    assert changes.quiet_candidate([recent | {"profileAgeDays": 49}], NOW, 30)      # profile created 51 days ago
    assert not changes.quiet_candidate([recent | {"profileAgeDays": 48}], NOW, 30)  # 50 days: too young
    assert not changes.quiet_candidate([old, _row(days_ago=8)], NOW, 30)            # nothing in the last 7 days
    assert not changes.quiet_candidate([old, _row(days_ago=2, status="rejected")], NOW, 30)
