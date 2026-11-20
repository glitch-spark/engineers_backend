"""Statistics → Interviews report: outcomes, period rows, groups, reasons, depth, follow-ups and the summary (pure)."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")
os.environ["RESUME_QUEUE_DISABLED"] = "1"

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from beanie import init_beanie
from bson import ObjectId
from mongomock_motor import AsyncMongoMockClient

from app.models import Interview
from app.stats import interview_report as ir

pytestmark = pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning")
UTC = ZoneInfo("UTC")
NOW = datetime(2026, 11, 20, 18)
START, END = datetime(2026, 10, 6), datetime(2026, 11, 21)
ACCT, OWNER = ObjectId(), ObjectId()
ACCOUNTS = {str(ACCT): {"name": "Alpha", "region": "EU", "archived": False, "ownerId": OWNER}}


@pytest.fixture(autouse=True)
async def _models():
    await init_beanie(database=AsyncMongoMockClient()["interview-report-unit"], document_models=[Interview])


def _at(month, day, hour=15):
    return datetime(2026, month, day, hour)


def _rnd(stage, at, status, **kw):
    return {"stage": stage, "at": at, "scheduledAt": at, "status": status, **kw}


def _facts(*rounds, app=None, account=ACCT, owner=OWNER, **kw):
    """An interview's facts from its history rounds (the tip mirrors the last one, as saved documents do)."""
    last = rounds[-1]
    iv = Interview.model_validate({"accountId": account, "createdBy": owner, "scheduledAt": last["scheduledAt"],
                                   "stage": last["stage"], "status": last["status"], "stageHistory": list(rounds), **kw})
    return ir.interview_facts(iv, ObjectId(), app, NOW)


def _rows(*facts, start=START, end=END, accounts=ACCOUNTS):
    return ir.period_rows(list(facts), accounts, start, end, NOW, UTC)


def _row(outcome="passed", **kw):
    """A period row as `period_rows` builds it, for the counting tests."""
    at = kw.pop("at", _at(11, 3))
    status = {"no_answer": "completed", "waiting": "completed"}.get(outcome, outcome)
    return {"iid": ObjectId(), "accountId": str(ACCT), "ownerId": OWNER, "profile": "Alpha", "region": None,
            "area": None, "scoreBand": None, "by": "unknown", "stage": "intro", "at": at,
            "end": at + timedelta(hours=1), "status": status, "reason": None, "caller": None, "reschedules": [],
            "followed": False, "statusAt": None, "outcome": outcome, "latest": True, "firstHeld": False,
            "offer": False, "ended": False, "heldCount": 1, "gapDays": None, "local": at.replace(tzinfo=UTC), **kw}


def _many(n, outcome="passed", **kw):
    return [_row(outcome, **kw) for _ in range(n)]


# ---- outcome ----
def test_outcome_completed_latest_waits_13_days_then_is_no_answer():
    end = NOW - timedelta(days=14)
    assert ir.outcome("completed", None, True, end, NOW) == "no_answer"
    assert ir.outcome("completed", None, True, end + timedelta(days=1), NOW) == "waiting"  # ended 13 days ago
    assert ir.outcome("completed", None, True, end + timedelta(seconds=1), NOW) == "waiting"


def test_outcome_rejected_no_answer_reason_vs_another():
    assert ir.outcome("rejected", "no_answer", True, NOW, NOW) == "no_answer"
    assert ir.outcome("rejected", "pay", True, NOW, NOW) == "rejected"
    assert ir.outcome("rejected", None, True, NOW, NOW) == "rejected"


def test_outcome_passed_by_status_or_by_a_later_round():
    old = NOW - timedelta(days=30)
    assert ir.outcome("passed", None, True, old, NOW) == "passed"
    assert ir.outcome("completed", None, False, old, NOW) == "passed"
    assert ir.outcome("rejected", "pay", False, old, NOW) == "passed"


def test_outcome_canceled_and_scheduled_win_over_a_later_round():
    assert ir.outcome("canceled", None, False, NOW, NOW) == "canceled"
    assert ir.outcome("scheduled", None, False, NOW, NOW) == "scheduled"


# ---- interview_facts / period_rows ----
def test_completed_round_followed_by_a_later_round_passed_whatever_the_later_status():
    rows = _rows(_facts(_rnd("intro", _at(10, 10), "completed"), _rnd("tech_round_1", _at(10, 15), "canceled")))
    assert [(r["stage"], r["outcome"]) for r in rows] == [("intro", "passed"), ("tech", "canceled")]


def test_ai_interview_and_home_assessment_ignored():
    # the home assessment after the intro is no "later round": the intro stays the latest
    rows = _rows(_facts(_rnd("ai_interview", _at(11, 1), "passed"), _rnd("intro", _at(11, 15), "completed"),
                        _rnd("home_assessment", _at(11, 18), "scheduled")))
    assert [(r["stage"], r["outcome"], r["latest"]) for r in rows] == [("intro", "waiting", True)]


def test_round_counts_by_its_own_date_but_is_judged_on_the_whole_interview():
    f = _facts(_rnd("intro", _at(10, 1), "completed"), _rnd("tech_round_1", _at(10, 10), "completed"),
               _rnd("final", _at(11, 25), "scheduled"))
    rows = _rows(f)  # intro before the period, final after it
    assert [(r["stage"], r["outcome"]) for r in rows] == [("tech", "passed")]
    assert rows[0]["firstHeld"] is False and rows[0]["heldCount"] == 2
    assert _rows(f, start=_at(10, 10), end=_at(10, 10) + timedelta(seconds=1)) != []
    assert _rows(f, start=_at(10, 9), end=_at(10, 10)) == []  # [start, end)


def test_profile_outside_the_accounts_is_dropped():
    assert _rows(_facts(_rnd("intro", _at(11, 3), "passed"), account=ObjectId())) == []


def test_company_counts_on_its_first_held_round_only():
    held_first = _rows(_facts(_rnd("intro", _at(11, 3), "completed"), _rnd("final", _at(11, 10), "passed")))
    assert [r["firstHeld"] for r in held_first] == [True, False]
    planned = _rows(_facts(_rnd("intro", _at(11, 25), "scheduled")), end=_at(12, 1))
    canceled = _rows(_facts(_rnd("intro", _at(11, 3), "canceled")))
    assert [r["firstHeld"] for r in planned + canceled] == [False, False]
    # canceled intro, then a held tech round: the company counts on the tech round
    later = _rows(_facts(_rnd("intro", _at(11, 3), "canceled"), _rnd("tech_round_1", _at(11, 5), "rejected")))
    assert [r["firstHeld"] for r in later] == [False, True]
    rep = ir.report(held_first + planned + canceled + later, NOW)
    assert rep["totals"]["companies"] == 2 <= rep["totals"]["held"] == 3


def test_offer_is_a_passed_final():
    rows = _rows(_facts(_rnd("intro", _at(11, 3), "passed"), _rnd("final", _at(11, 10), "passed")),
                 _facts(_rnd("final", _at(11, 10), "rejected", statusReason="pay")),
                 _facts(_rnd("final", _at(11, 11), "completed")))
    assert [r["offer"] for r in rows] == [False, True, False, False]
    assert ir.report(rows, NOW)["totals"]["offers"] == 1


def test_caller_round_needs_an_enabled_named_caller():
    def who(caller):
        return _rows(_facts(_rnd("intro", _at(11, 3), "passed", **({"caller": caller} if caller else {}))))[0]["caller"]

    assert who(None) is None
    assert who({"enabled": False, "callerName": "Cal", "callerId": "c1"}) is None
    assert who({"enabled": True, "callerName": "TBD"}) is None
    assert who({"enabled": True, "callerName": "Cal", "callerId": "c1"}) == ("c1", "Cal")
    assert who({"enabled": True, "callerName": " Cal "}) == ("name:cal", "Cal")


def test_bid_side_keys_from_the_applications_row_then_the_interview():
    r = _rnd("intro", _at(11, 3), "passed")
    app = {"by": "bidder", "scoreBand": "good", "area": "Backend"}
    link = {"kind": "tailored", "score": {"total": 85, "band": "strong"}}
    row = _rows(_facts(r, app=app, resumeLink=link, appliedBy={"kind": "self"}))[0]
    assert (row["by"], row["scoreBand"], row["area"]) == ("bidder", "good", "Backend")
    row = _rows(_facts(r, resumeLink=link, appliedBy={"kind": "self"}))[0]
    assert (row["by"], row["scoreBand"], row["area"]) == ("self", "strong", None)
    row = _rows(_facts(r, app={"by": "unknown"}, appliedBy={"kind": "bidder"}, fitScore={"total": 55, "band": "fair"}))[0]
    assert (row["by"], row["scoreBand"]) == ("bidder", "fair")
    row = _rows(_facts(r))[0]
    assert (row["by"], row["scoreBand"], row["area"]) == ("unknown", None, None)


# ---- groups ----
def test_group_counts_and_shape():
    rows = (_many(3) + _many(2, "rejected") + _many(1, "no_answer") + _many(4, "waiting") + _many(2, "canceled")
            + _many(1, "scheduled"))
    t = ir.report(rows, NOW)["totals"]
    assert t == {"key": "all", "label": "All rounds", "rounds": 13, "held": 10, "decided": 6, "passed": 3,
                 "rejected": 2, "noAnswer": 1, "waiting": 4, "canceled": 2, "scheduled": 1, "rate": 50.0, "few": True,
                 "mark": None, "companies": 0, "offers": 0}


def test_pass_rate_and_few_at_19_vs_20_decided():
    rep = ir.report(_many(6, scoreBand="strong") + _many(13, "rejected", scoreBand="strong") + _many(5, "waiting", scoreBand="strong")
                    + _many(7, scoreBand="good") + _many(13, "no_answer", scoreBand="good"), NOW)
    score = {g["key"]: g for g in rep["byScore"]}
    assert (score["strong"]["decided"], score["strong"]["rate"], score["strong"]["few"]) == (19, 31.6, True)
    assert (score["good"]["decided"], score["good"]["rate"], score["good"]["few"]) == (20, 35.0, False)
    assert score["fair"]["rate"] is None and score["fair"]["few"] is True
    assert [(g["key"], g["label"]) for g in rep["byScore"]] == [
        ("strong", "80+"), ("good", "65–79"), ("fair", "50–64"), ("weak", "Under 50"), ("unknown", "No score")]
    assert ir.pass_rate(_many(1) + _many(2, "rejected") + _many(9, "waiting")) == 100 / 3
    assert ir.pass_rate(_many(3, "waiting")) is None


def test_stage_who_weekday_hour_region_area_applied_by_tables():
    rows = [_row(stage="final", at=_at(11, 2, 9), local=_at(11, 2, 9).replace(tzinfo=UTC), region="EU", area="Backend",
                 by="self"),                                                        # Monday morning
            _row("rejected", stage="intro", local=_at(11, 4, 12).replace(tzinfo=UTC), region="US", by="bidder",
                 caller=("c1", "Cal")),                                             # Wednesday 12:00
            _row(stage="intro", local=_at(11, 4, 16).replace(tzinfo=UTC), region="US"),
            _row(stage="tech", local=_at(11, 8, 17).replace(tzinfo=UTC), region="Mars")]  # Sunday 17:00
    rep = ir.report(rows, NOW)

    def table(name):
        return [(g["key"], g["label"], g["rounds"]) for g in rep[name]]

    assert table("byStage") == [("intro", "Intro", 2), ("tech", "Tech", 1), ("final", "Final", 1)]
    assert table("byWho") == [("self", "Self", 3), ("caller", "Caller", 1)]
    assert table("byWeekday") == [("mon", "Mon", 1), ("wed", "Wed", 2), ("sun", "Sun", 1)]
    assert table("byHour") == [("morning", "Morning", 1), ("afternoon", "Afternoon", 2), ("evening", "Evening", 1)]
    assert table("byRegion") == [("EU", "EU", 1), ("US", "US", 2), ("unknown", "Unknown", 1)]
    assert table("byArea") == [("Backend", "Backend", 1), ("unknown", "Unknown", 3)]
    assert table("byAppliedBy") == [("self", "Self bid", 1), ("bidder", "Bidder bid", 1), ("unknown", "Unknown", 2)]
    empty = ir.report([], NOW)
    assert [g["key"] for g in empty["byWho"]] == ["self", "caller"] and empty["byStage"] == empty["byWeekday"] == []
    assert [g["key"] for g in empty["byAppliedBy"]] == ["self", "bidder"]


def test_few_callers_and_profiles_fold_into_other_with_members():
    big = _many(20, caller=("c1", "Cal"), accountId="a1", profile="Alpha")
    small = _many(19, caller=("c2", "Dee"), accountId="a2", profile="Beta") + _many(2, "waiting", caller=("c3", "Eve"),
                                                                                    accountId="a3", profile="Gamma")
    rep = ir.report(big + small + _many(5), NOW)  # 5 self rounds on the default profile
    callers = rep["byCaller"]
    assert [(g["key"], g["label"], g["rounds"]) for g in callers] == [("c1", "Cal", 20), ("other", "Other callers", 21)]
    assert "members" not in callers[0]
    assert callers[1]["memberCount"] == 2 and callers[1]["decided"] == 19 and callers[1]["few"] is True
    assert [(m["key"], m["label"], m["rounds"], m["few"]) for m in callers[1]["members"]] == [
        ("c2", "Dee", 19, True), ("c3", "Eve", 2, True)]
    profiles = rep["byProfile"]
    assert [(g["key"], g["label"]) for g in profiles] == [("a1", "Alpha"), ("other", "Other profiles")]
    assert profiles[1]["memberCount"] == 3 and profiles[1]["few"] is False  # 24 decided together
    none_few = ir.report(big, NOW)
    assert [g["key"] for g in none_few["byCaller"]] == ["c1"] and [g["key"] for g in none_few["byProfile"]] == ["a1"]


# ---- reasons ----
def test_reason_lists_count_share_and_unfilled_last():
    rows = (_many(3, "rejected", reason="pay") + _many(1, "rejected", reason="experience") + _many(4, "rejected")
            + _many(2, "no_answer", status="rejected", reason="no_answer")
            + _many(2, "canceled", reason="company") + _many(1, "canceled", reason="bogus")
            + [_row(reschedules=["me", "company"]), _row("canceled", reason="company", reschedules=["me"])])
    r = ir.report(rows, NOW)["reasons"]
    assert r["rejected"] == [
        {"key": "pay", "label": "Pay or contract terms", "count": 3, "share": 37.5},
        {"key": "experience", "label": "Experience didn't match (stack, seniority, domain)", "count": 1, "share": 12.5},
        {"key": "none", "label": "Reason not filled in yet", "count": 4, "share": 50.0}]
    assert r["canceled"] == [{"key": "company", "label": "Company", "count": 3, "share": 75.0},
                             {"key": "none", "label": "Reason not filled in yet", "count": 1, "share": 25.0}]
    assert r["rescheduled"] == [{"key": "me", "label": "Me", "count": 2, "share": 66.7},
                                {"key": "company", "label": "Company", "count": 1, "share": 33.3}]


def test_reschedule_rate_ignores_rounds_before_the_log_started():
    def on(day, hour, n=0):
        return _row(local=_at(11, day, hour).replace(tzinfo=UTC), reschedules=["me"] * n)

    rate = ir.report([on(19, 23, 1), on(19, 10), on(20, 0, 2), on(20, 9), on(21, 9, 1), on(22, 9)], NOW)["reasons"][
        "rescheduleRate"]
    assert rate == {"rescheduled": 2, "rounds": 4, "since": "2026-11-20"}
    assert ir.report([], NOW)["reasons"] == {"canceled": [], "rescheduled": [], "rejected": [],
                                             "rescheduleRate": {"rescheduled": 0, "rounds": 0, "since": "2026-11-20"}}


# ---- depth ----
def test_depth_counts_ended_interviews_by_held_rounds():
    one = _facts(_rnd("intro", _at(11, 3), "rejected", statusReason="pay"))
    two = _facts(_rnd("intro", _at(10, 10), "passed"), _rnd("tech_round_1", _at(10, 20), "completed"))  # no answer
    offer = _facts(_rnd("intro", _at(10, 10), "passed"), _rnd("tech_round_1", _at(10, 12), "canceled"),
                   _rnd("tech_round_2", _at(10, 14), "passed"), _rnd("panel", _at(10, 16), "passed"),
                   _rnd("final", _at(10, 20), "passed"))
    open_ = _facts(_rnd("intro", _at(11, 3), "passed"), _rnd("tech_round_1", _at(11, 18), "completed"))  # waiting
    passed_tip = _facts(_rnd("intro", _at(11, 3), "passed"))  # passed, not a final: not ended
    d = ir.report(_rows(one, two, offer, open_, passed_tip), NOW)["depth"]
    assert d["ended"] == 3 and d["avgRounds"] == 2.3  # 1, 2 and 4 held rounds
    assert d["endedAfter"] == [{"label": "1 round", "count": 1}, {"label": "2 rounds", "count": 1},
                               {"label": "3 or more", "count": 1}]
    # one interview, counted once, by all its held rounds even when only one round is in the period
    late = ir.report(_rows(two, start=_at(10, 15)), NOW)["depth"]
    assert late["ended"] == 1 and late["avgRounds"] == 2.0


def test_days_between_rounds_skip_canceled_and_need_the_later_round_in_the_period():
    f = _facts(_rnd("intro", _at(10, 1), "passed"), _rnd("tech_round_1", _at(10, 8), "canceled"),
               _rnd("tech_round_2", _at(10, 11, 3), "passed"), _rnd("final", _at(10, 13, 3), "scheduled"))
    g = _facts(_rnd("intro", _at(10, 20), "passed"), _rnd("final", _at(10, 24), "passed"))
    d = ir.report(_rows(f, g), NOW)["depth"]["daysBetweenRounds"]
    assert d == {"median": 4.0, "n": 3}  # 9.5 (Oct 1 → Oct 11, 03:00), 2 and 4 days
    assert ir.report([], NOW)["depth"] == {
        "ended": 0, "avgRounds": None,
        "endedAfter": [{"label": "1 round", "count": 0}, {"label": "2 rounds", "count": 0},
                       {"label": "3 or more", "count": 0}],
        "daysBetweenRounds": {"median": None, "n": 0}, "daysToAnswer": {"median": None, "n": 0}}


def test_days_to_answer_uses_only_rounds_with_status_at():
    def r(status, days, **kw):
        at = _at(11, 2)
        return _rnd("intro", at, status, endsAt=at + timedelta(hours=1),
                    **({"statusAt": at + timedelta(hours=1, days=days)} if days is not None else {}), **kw)

    rows = _rows(_facts(r("passed", 2)), _facts(r("rejected", 5, statusReason="pay")), _facts(r("passed", None)),
                 _facts(r("rejected", 9, statusReason="no_answer")),   # gave up waiting: not an answer
                 _facts(r("completed", 0)))                             # marked held: not an answer
    assert ir.report(rows, NOW)["depth"]["daysToAnswer"] == {"median": 3.5, "n": 2}


# ---- follow-ups ----
def test_follow_ups_followed_vs_left_without_a_result_for_a_week():
    end = NOW - timedelta(days=7)

    def left(**kw):
        return _row(kw.pop("outcome", "waiting"), end=kw.pop("end", end), **kw)

    rows = [
        _row(followed=True), _row("rejected", followed=True), _row("canceled", followed=True),   # canceled: not held
        left(),                                                    # still completed, ended 7 days ago
        left(end=end + timedelta(seconds=1)),                      # not yet 7 days
        left(outcome="no_answer", end=NOW - timedelta(days=20)),
        left(outcome="passed", status="completed", latest=False),  # a later round came: it had a result
        left(outcome="passed", status="passed", statusAt=end + timedelta(days=7)),      # result after 7 days
        left(outcome="rejected", status="rejected", statusAt=end + timedelta(days=6)),  # result within the week
        left(outcome="passed", status="passed"),                   # no statusAt and no longer completed
    ]
    assert ir.report(rows, NOW)["followUps"] == [
        {"key": "followed_up", "label": "Followed up", "rounds": 2, "passed": 1, "rejected": 1, "noAnswer": 0,
         "waiting": 0},
        {"key": "not_followed_up", "label": "Not followed up", "rounds": 3, "passed": 1, "rejected": 0, "noAnswer": 1,
         "waiting": 1}]


# ---- summary ----
A, B, C, D, BOSS = (ObjectId() for _ in range(5))
NAMES = {A: "Ana", B: "Ben", C: "Cy", D: "Dee"}


def _person(owner, passed, rejected=0, companies=0, offers=0, extra=()):
    rows = _many(passed, ownerId=owner) + _many(rejected, "rejected", ownerId=owner) + [
        _row(o, ownerId=owner) for o in extra]
    for r in rows[:companies]:
        r["firstHeld"] = True
    for r in rows[:offers]:
        r["offer"] = True
    return rows


def _team():
    return (_person(A, 15, 5, companies=6, offers=2, extra=("waiting", "canceled", "scheduled", "scheduled"))  # 75.0
            + _person(B, 8, 12, companies=9, offers=1)                                                         # 40.0
            + _person(C, 3, 1, companies=4))                                                                   # few
    # Dee has no rounds


def test_summary_me_cards_rank_against_people_with_a_round_held():
    team = _team()
    mine = [r for r in team if r["ownerId"] == A]
    s = ir.summary(team, NAMES, set(NAMES), A, False, ir.report(mine, NOW)["totals"])
    assert s["cards"] == {
        "companies": {"value": 6, "rank": 2, "of": 3, "median": 6, "vsMedian": 0.0},
        "rounds": {"value": 24, "held": 21, "canceled": 1, "scheduled": 2, "rank": 1, "of": 3, "median": 20,
                   "vsMedian": 0.05},
        "passRate": {"value": 75.0, "passed": 15, "decided": 20, "waiting": 1, "rank": 1, "of": 2, "teamRate": 59.1},
        "offers": {"value": 2, "rank": 1, "of": 3}}


def test_summary_pass_rate_ranked_only_from_20_decided():
    team = _team()
    s = ir.summary(team, NAMES, set(NAMES), C, False, ir.report([r for r in team if r["ownerId"] == C], NOW)["totals"])
    assert s["cards"]["passRate"] == {"value": 75.0, "passed": 3, "decided": 4, "waiting": 0, "rank": None, "of": 2,
                                      "teamRate": 59.1}
    by_name = {e["name"]: e for e in s["everyone"]}
    assert by_name["Cy"]["few"] is True and by_name["Cy"]["ranks"]["passRate"] is None
    assert by_name["Ben"]["few"] is False and by_name["Ben"]["ranks"] == {
        "companies": 1, "held": 2, "passRate": 2, "offers": 2}


def test_summary_everyone_lists_people_with_a_round_held():
    s = ir.summary(_team(), NAMES, set(NAMES), A, False, {})
    assert [(e["name"], e["companies"], e["held"], e["decided"], e["passed"], e["passRate"], e["offers"],
             e["isSubject"]) for e in s["everyone"]] == [
        ("Ben", 9, 20, 20, 8, 40.0, 1, False), ("Ana", 6, 21, 20, 15, 75.0, 2, True), ("Cy", 4, 4, 4, 3, 75.0, 0, False)]
    assert all(e["userId"] == str(u) for e, u in zip(s["everyone"], (B, A, C)))


def test_summary_subject_without_rounds_and_non_member_subject():
    s = ir.summary(_team(), NAMES, set(NAMES), D, False, {})
    assert s["cards"]["companies"] == {"value": 0, "rank": 4, "of": 4, "median": 6, "vsMedian": -1.0}
    assert s["cards"]["passRate"]["value"] is None and s["cards"]["passRate"]["rank"] is None
    # an admin's rounds are in no one's rank or median
    team = _team() + _person(BOSS, 30, companies=30)
    s = ir.summary(team, {**NAMES, BOSS: "Boss"}, set(NAMES), BOSS, False, {})
    assert s["cards"]["companies"] == {"value": 30, "rank": 1, "of": 4, "median": 6, "vsMedian": 4.0}
    assert [e["name"] for e in s["everyone"]][0] == "Boss" and {e["ranks"]["companies"] for e in s["everyone"]} == {1, 2, 3}


def test_summary_team_view_totals_and_per_person_medians():
    team = _team()
    s = ir.summary(team, NAMES, set(NAMES), A, True, ir.report(team, NOW)["totals"])
    assert s["cards"] == {
        "companies": {"value": 19, "rank": None, "of": None, "median": 6, "vsMedian": None},
        "rounds": {"value": 48, "held": 45, "canceled": 1, "scheduled": 2, "rank": None, "of": None, "median": 20,
                   "vsMedian": None},
        "passRate": {"value": 59.1, "passed": 26, "decided": 44, "waiting": 1, "rank": None, "of": None,
                     "teamRate": 59.1},
        "offers": {"value": 3, "rank": None, "of": None}}


def test_summary_empty():
    s = ir.summary([], NAMES, set(NAMES), A, False, {})
    assert s["everyone"] == []
    assert s["cards"] == {
        "companies": {"value": 0, "rank": 1, "of": 1, "median": None, "vsMedian": None},
        "rounds": {"value": 0, "held": 0, "canceled": 0, "scheduled": 0, "rank": 1, "of": 1, "median": None,
                   "vsMedian": None},
        "passRate": {"value": None, "passed": 0, "decided": 0, "waiting": 0, "rank": None, "of": 0, "teamRate": None},
        "offers": {"value": 0, "rank": 1, "of": 1}}
