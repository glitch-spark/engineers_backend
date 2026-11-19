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


def test_team_baseline_median_over_profiles_with_30_matured():
    rows = _prof(A, 30, 3) + _prof(B, 30, 1) + _prof(ObjectId(), 10, 9)
    t = team_baseline(summarize(rows, CUT), rows)
    assert t == {"per100": 6.7, "medianDays": 4, "profiles": 2}


def test_team_baseline_none_when_no_profile_qualifies():
    rows = _prof(A, 10, 1)
    assert team_baseline(summarize(rows, CUT), rows) == {"per100": None, "medianDays": 4, "profiles": 0}


TEAM = {"per100": 2.6, "medianDays": 6, "profiles": 3}


def _s(matured, mi=0, per100=None, reach=(0, 0, 0), median=None):
    return {"matured": matured, "maturedInterviews": mi, "interviews": reach[0], "per100": per100,
            "days": {"first": 1, "median": median, "slowest": 30} if median else None,
            "reach": dict(zip(("intro", "tech", "final"), reach))}


def test_grade_dead_150_matured_no_interview():
    assert grade(_s(150), TEAM, None, 14, NOW) == ("dead", ["150+ matured bids, no interview"])


def test_grade_dead_silent_while_bidding():
    act = {"firstAt": NOW - timedelta(days=100), "lastAt": NOW - timedelta(days=2), "lastRoundAt": NOW - timedelta(days=30)}
    assert grade(_s(40, 1, 2.5), TEAM, act, 14, NOW) == ("dead", ["no interview for 21+ days"])
    act["lastRoundAt"] = None
    assert grade(_s(40), TEAM, act, 14, NOW)[0] == "dead"


def test_grade_not_dead_when_new_profile():
    act = {"firstAt": NOW - timedelta(days=30), "lastAt": NOW, "lastRoundAt": None}
    assert grade(_s(40, 1, 2.5), TEAM, act, 14, NOW)[0] != "dead"


def test_young_cohort_is_early_not_judged():
    assert grade(_s(10), TEAM, None, 14, NOW) == ("early", ["too early to judge"])
    assert grade(_s(50, 1, 2.0), {"per100": None, "medianDays": None, "profiles": 0}, None, 14, NOW)[0] == "early"


def test_grade_strong_needs_tech():
    assert grade(_s(50, 2, 4.0, (2, 1, 0)), TEAM, None, 14, NOW) == ("strong", [])
    assert grade(_s(50, 2, 4.0, (2, 0, 0)), TEAM, None, 14, NOW)[0] == "watch"


def test_grade_watch_reasons():
    g, why = grade(_s(80, 1, 1.2, (3, 0, 0), median=14), TEAM, None, 14, NOW)
    assert g == "watch"
    assert why == ["below team (1.2 vs 2.6 per 100)", "slow (median 14 days vs team 6)", "intros don't reach tech"]
