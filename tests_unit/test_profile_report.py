from datetime import datetime, timedelta

from bson import ObjectId

from app.stats.profile_report import grade, summarize, team_baseline

NOW = datetime(2026, 11, 18, 12)
CUT = NOW - timedelta(days=14)
A, B = ObjectId(), ObjectId()


def row(acc=A, age=30, by="bidder", status="approved", iv=None, days=None, furthest=None):
    return {"accountId": acc, "appliedAt": NOW - timedelta(days=age), "by": by, "bidStatus": status,
            "interviewId": ObjectId() if iv else None, "daysToInterview": days, "furthest": furthest}


def test_summarize_counts_self_bidder_matured_and_in_play():
    rows = [row(by="self"), row(by="bidder"), row(by="bidder", age=3), row(age=3, iv=True, furthest="intro", days=1)]
    s = summarize(rows, CUT)[str(A)]
    assert (s["bidsSelf"], s["bidsBidder"], s["matured"], s["stillInPlay"], s["interviews"],
            s["maturedInterviews"]) == (1, 3, 2, 1, 1, 0)


def test_rejected_rows_ignored():
    assert summarize([row(status="rejected", iv=True, furthest="intro", days=1)], CUT) == {}


def test_rows_without_account_skipped():
    assert summarize([row(acc=None)], CUT) == {}


def test_per100_one_decimal_and_none_without_matured():
    rows = [row(iv=True, furthest="intro", days=1)] + [row() for _ in range(6)]
    assert summarize(rows, CUT)[str(A)]["per100"] == 14.3
    assert summarize([row(age=2)], CUT)[str(A)]["per100"] is None


def test_days_first_median_slowest():
    r = lambda d: row(iv=True, furthest="intro", days=d)
    assert summarize([r(13), r(2), r(5)], CUT)[str(A)]["days"] == {"first": 2, "median": 5, "slowest": 13}
    assert summarize([r(2), r(5), r(7), r(9)], CUT)[str(A)]["days"]["median"] == 5
    assert summarize([row()], CUT)[str(A)]["days"] is None


def test_reach_buckets():
    rows = [row(iv=True, furthest=f, days=1) for f in ("intro", "tech", "hiring", "final", "offer")]
    assert summarize(rows, CUT)[str(A)]["reach"] == {"intro": 5, "tech": 4, "final": 2}


def _mk(n, ivs):
    return [row(acc=ObjectId(), iv=i < ivs, furthest="intro", days=4) for i in range(n)]


def _prof(acc, n, ivs):
    return [row(acc=acc, iv=i < ivs, furthest="intro", days=4) for i in range(n)]


def test_team_baseline_is_pooled_rate_over_all_matured_rows():
    rows = _prof(A, 30, 3) + _prof(B, 30, 1) + _prof(ObjectId(), 10, 9)
    t = team_baseline(summarize(rows, CUT), rows)
    assert (t["per100"], t["medianDays"], t["profiles"]) == (18.6, 4, 2)  # 13 / 70, small profiles pooled too


def test_team_baseline_pooled_not_collapsed_by_zero_profiles():
    rows = _prof(A, 40, 2) + _prof(B, 40, 0) + _prof(ObjectId(), 40, 0)
    assert team_baseline(summarize(rows, CUT), rows)["per100"] == 1.7  # median of profiles would be 0.0


def test_team_baseline_none_when_nothing_matured():
    rows = [row(age=3, iv=True, furthest="intro", days=1)]
    t = team_baseline(summarize(rows, CUT), rows)
    assert (t["per100"], t["profiles"]) == (None, 0)


def test_expected_not_zeroed_by_display_rounding():
    team_rows = _prof(A, 5000, 2)  # pooled 0.04 per 100 -> displays 0.0
    team = team_baseline(summarize(team_rows, CUT), team_rows)
    assert team["per100"] == 0.0 and team["rate"] == 0.04
    g, why = grade(summarize(_prof(B, 10000, 0), CUT)[str(B)], team)
    assert g == "dead" and "about 4" in why[0]


TEAM = {"per100": 2.6, "medianDays": 6, "profiles": 3}
REAL = {"per100": 0.5, "medianDays": 6, "profiles": 9}  # the real team rate: ~0.5 interviews per 100 matured bids


def _s(matured, mi=0, per100=None, reach=(0, 0, 0), median=None):
    return {"matured": matured, "maturedInterviews": mi, "interviews": reach[0], "per100": per100,
            "days": {"first": 1, "median": median, "slowest": 30} if median is not None else None,
            "reach": dict(zip(("intro", "tech", "final"), reach))}


def test_grade_dead_when_team_rate_expects_3_or_more():
    assert grade(_s(600, 0, 0.0), REAL) == (
        "dead", ["no interview from 600 matured bids (the team rate would give about 3)"])


def test_grade_not_dead_when_expected_below_3():
    assert grade(_s(150, 0, 0.0), REAL) == (
        "early", ["too few to tell (150 matured bids; about 0.8 interviews expected at the team rate)"])


def test_below_team_suppressed_when_expected_below_3():
    assert grade(_s(300, 1, 0.3, (1, 0, 0)), REAL) == (
        "early", ["too few to tell (300 matured bids; about 1.5 interviews expected at the team rate)"])


def test_below_team_shown_when_expected_3_or_more():
    assert grade(_s(800, 1, 0.1, (1, 0, 0)), REAL) == ("watch", ["below team (0.1 vs 0.5 per 100)"])


def test_strong_with_small_sample():
    assert grade(_s(50, 2, 4.0, (2, 1, 0)), REAL) == ("strong", [])


def test_slow_needs_team_median_of_at_least_1_day():
    zero = {**REAL, "medianDays": 0}
    assert grade(_s(50, 1, 2.0, (1, 1, 0), median=5), zero) == ("strong", [])
    assert grade(_s(50, 1, 2.0, (1, 0, 0), median=5), zero) == ("watch", ["no interview has reached tech yet"])
    one = {**REAL, "medianDays": 1}
    assert grade(_s(50, 1, 2.0, (1, 0, 0), median=2), one) == (
        "watch", ["slow (median 2 days vs team 1)", "no interview has reached tech yet"])
    assert grade(_s(50, 0, 0.0, (1, 0, 0), median=2), {**one, "medianDays": None})[0] == "early"


def test_young_cohort_is_early_not_judged():
    assert grade(_s(10), TEAM) == ("early", ["too early to judge (10 of 30 matured bids)"])
    assert grade(_s(50, 1, 2.0), {"per100": None, "medianDays": None, "profiles": 0})[0] == "early"


def test_grade_strong_needs_tech():
    assert grade(_s(50, 2, 4.0, (2, 1, 0)), TEAM) == ("strong", [])
    assert grade(_s(50, 2, 4.0, (2, 0, 0)), TEAM) == ("watch", ["no interview has reached tech yet"])


def test_grade_watch_reasons():
    g, why = grade(_s(200, 2, 1.0, (3, 0, 0), median=14), TEAM)
    assert g == "watch"
    assert why == ["below team (1.0 vs 2.6 per 100)", "slow (median 14 days vs team 6)", "intros don't reach tech"]


def test_early_reasons_distinguish_young_profile_from_missing_baseline():
    none_team = {"per100": None, "medianDays": None, "profiles": 0}
    assert grade(_s(50, 1, 2.0), none_team) == (
        "early", ["no team baseline yet (fewer than 30 matured bids on every profile)"])
