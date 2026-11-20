"""GET /stats/interviews-report: scope, region, period, the applications join and the response shape (mongomock)."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")
os.environ["RESUME_QUEUE_DISABLED"] = "1"

from datetime import datetime

import pytest
from beanie import init_beanie
from bson import ObjectId
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from mongomock_motor import AsyncMongoMockClient

from app.deps import get_current_user
from app.models import Account, Application, DailyPlan, Interview, InterviewQuestion, UserExtra
from app.models.bid import Bid
from app.models.bidder import Bidder
from app.models.weekly_plan import WeeklyPlan
from app.routers import stats
from app.stats import profile_loader

pytestmark = [
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]
Q = "?period=custom&date=2026-10-06&to=2026-10-31"
GROUP_KEYS = {"key", "label", "rounds", "held", "decided", "passed", "rejected", "noAnswer", "waiting", "canceled",
              "scheduled", "rate", "few", "mark"}
TABLES = ("byStage", "byWho", "byCaller", "byProfile", "byRegion", "byArea", "byScore", "byAppliedBy", "byWeekday",
          "byHour")


def _d(day, hour=15):
    return datetime(2026, 10, day, hour)


@pytest.fixture
async def db():
    profile_loader.clear_cache()
    await init_beanie(database=AsyncMongoMockClient()["interviews-report-unit"], document_models=[
        UserExtra, Account, DailyPlan, Interview, InterviewQuestion, Bid, Bidder, WeeklyPlan, Application])
    yield
    profile_loader.clear_cache()


async def _user(name, role="staff"):
    return await UserExtra(name=name, email=f"{name.lower()}@x.io", role=role).insert()


async def _acct(owner, name, region="EU", archived=False):
    return await Account(name=name, region=region, createdBy=owner.id, template="x", archived=archived).insert()


async def _iv(acct, owner, *rounds, **kw):
    """An interview with history rounds (stage, at, status[, extra fields]); the tip mirrors the last one."""
    hist = [{"id": ObjectId().binary.hex(), "stage": s, "at": at, "scheduledAt": at, "status": st, **(x[0] if x else {})}
            for s, at, st, *x in rounds]
    doc = {"accountId": acct.id, "createdBy": owner.id, "scheduledAt": hist[-1]["scheduledAt"],
           "stage": hist[-1]["stage"], "status": hist[-1]["status"], "stageHistory": hist,
           "createdAt": hist[0]["at"], "updatedAt": hist[-1]["at"], "transcript": "long text", **kw}
    return (await Interview.get_motor_collection().insert_one(doc)).inserted_id


async def _many(acct, owner, n, status="passed", day=7, stage="intro", **extra):
    for _ in range(n):
        await _iv(acct, owner, (stage, _d(day), status, extra))


async def _get(user, qs=Q, fresh=True):
    if fresh:
        profile_loader.clear_cache()
    app = FastAPI()
    app.include_router(stats.router)
    app.dependency_overrides[get_current_user] = lambda: user
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        return await c.get("/stats/interviews-report" + qs)


async def _team():
    """Ana (EU + US profiles) and Ben (EU, one archived), with an admin's interview on Ana's EU profile."""
    ana, ben, boss = await _user("Ana"), await _user("Ben"), await _user("Boss", "admin")
    a_eu, a_us = await _acct(ana, "Alpha"), await _acct(ana, "AlphaUs", "US")
    b_eu, b_gone = await _acct(ben, "Beta"), await _acct(ben, "Gone", archived=True)
    cal = {"caller": {"enabled": True, "callerName": "Cal", "callerId": "c1"}}
    await _iv(a_eu, ana, ("intro", _d(7), "passed"),
              ("tech_round_1", _d(9), "rejected", {"statusReason": "pay", **cal}))
    await _iv(a_eu, ana, ("intro", _d(8), "canceled", {"statusReason": "company"}))
    await _iv(a_us, ana, ("intro", _d(8), "passed"), ("final", _d(12), "passed"))
    await _iv(b_eu, ben, ("intro", _d(7), "rejected"))
    await _iv(b_gone, ben, ("intro", _d(10), "completed"))  # long ago: no answer
    await _iv(a_eu, boss, ("intro", _d(11), "passed"))
    return ana, ben, boss, a_eu


def _blocks(out):
    """The number of rounds each block of the response covers."""
    t = out["totals"]
    assert all(sum(g["rounds"] for g in out[by]) == t["rounds"] for by in TABLES if by != "byCaller")
    assert out["summary"]["cards"]["rounds"]["value"] == t["rounds"]
    assert sum(r["count"] for r in out["reasons"]["canceled"]) == t["canceled"]
    assert sum(r["count"] for r in out["reasons"]["rejected"]) == t["rejected"]
    return t["rounds"], t["companies"], t["offers"], sum(g["rounds"] for g in out["byCaller"]), out["depth"]["ended"]


async def test_empty_period_returns_the_full_shape_with_zeros(db):
    ana = await _user("Ana")
    r = await _get(ana, "?period=custom&date=2026-10-06&to=2026-10-31&scope=me")
    assert r.status_code == 200
    zero = {"rounds": 0, "held": 0, "decided": 0, "passed": 0, "rejected": 0, "noAnswer": 0, "waiting": 0,
            "canceled": 0, "scheduled": 0, "rate": None, "few": True, "mark": None}

    def empty(*pairs):
        return [{"key": k, "label": label, **zero} for k, label in pairs]

    assert r.json() == {
        "window": {"start": "2026-10-06", "end": "2026-10-31", "startClamped": False},
        "totals": {"key": "all", "label": "All rounds", **zero, "companies": 0, "offers": 0},
        "summary": {"cards": {
            "companies": {"value": 0, "rank": 1, "of": 1, "median": None, "vsMedian": None},
            "rounds": {"value": 0, "held": 0, "canceled": 0, "scheduled": 0, "rank": 1, "of": 1, "median": None,
                       "vsMedian": None},
            "passRate": {"value": None, "passed": 0, "decided": 0, "waiting": 0, "rank": None, "of": 0,
                         "teamRate": None},
            "offers": {"value": 0, "rank": 1, "of": 1}}, "everyone": []},
        "findings": [{"kind": "none", "text": "No clear pattern yet in this period — try a longer date range."}],
        "byStage": [], "byWho": empty(("self", "Self"), ("caller", "Caller")), "byCaller": [], "byProfile": [],
        "byRegion": [], "byArea": [],
        "byScore": empty(("strong", "80+"), ("good", "65–79"), ("fair", "50–64"), ("weak", "Under 50"),
                         ("unknown", "No score")),
        "byAppliedBy": empty(("self", "Self bid"), ("bidder", "Bidder bid")), "byWeekday": [],
        "byHour": empty(("morning", "Morning"), ("afternoon", "Afternoon"), ("evening", "Evening")),
        "reasons": {"canceled": [], "rescheduled": [], "rejected": [],
                    "rescheduleRate": {"rescheduled": 0, "rounds": 0, "since": "2026-11-20"}},
        "depth": {"ended": 0, "avgRounds": None,
                  "endedAfter": [{"label": "1 round", "count": 0}, {"label": "2 rounds", "count": 0},
                                 {"label": "3 or more", "count": 0}],
                  "daysBetweenRounds": {"median": None, "n": 0}, "daysToAnswer": {"median": None, "n": 0}},
        "followUps": [
            {"key": "followed_up", "label": "Followed up", "rounds": 0, "passed": 0, "rejected": 0, "noAnswer": 0,
             "waiting": 0},
            {"key": "not_followed_up", "label": "Not followed up", "rounds": 0, "passed": 0, "rejected": 0,
             "noAnswer": 0, "waiting": 0}]}


async def test_scope_and_region_apply_to_every_block(db):
    ana, ben, boss, _ = await _team()
    # (rounds, companies, offers, caller rounds, ended interviews)
    assert _blocks((await _get(ana, Q + "&scope=team")).json()) == (8, 5, 1, 1, 4)
    assert _blocks((await _get(ana, Q)).json()) == (8, 5, 1, 1, 4)  # Whole team is the default
    assert _blocks((await _get(ana, Q + "&scope=team&region=EU")).json()) == (6, 4, 0, 1, 3)
    assert _blocks((await _get(ana, Q + "&scope=team&region=US")).json()) == (2, 1, 1, 0, 1)
    assert _blocks((await _get(ana, Q + "&scope=me")).json()) == (5, 2, 1, 1, 2)
    assert _blocks((await _get(ana, Q + "&scope=me&region=EU")).json()) == (3, 1, 0, 1, 1)
    assert _blocks((await _get(ben, Q + "&scope=me")).json()) == (2, 2, 0, 0, 2)  # the archived profile counts
    assert _blocks((await _get(ana, Q + "&scope=me&region=Latam")).json()) == (0, 0, 0, 0, 0)
    me = (await _get(ana, Q + "&scope=me&region=EU")).json()
    assert [g["label"] for g in me["byRegion"]] == ["EU"] and [g["key"] for g in me["byStage"]] == ["intro", "tech"]
    assert me["reasons"]["rejected"] == [{"key": "pay", "label": "Pay or contract terms", "count": 1, "share": 100.0}]
    assert [g["label"] for g in me["byCaller"][0]["members"]] == ["Cal"]


async def test_team_rate_is_the_whole_teams_in_both_scopes(db):
    ana, ben, _, _ = await _team()
    me = (await _get(ana, Q + "&scope=me&region=EU")).json()["summary"]["cards"]["passRate"]
    team = (await _get(ana, Q + "&scope=team&region=EU")).json()["summary"]["cards"]["passRate"]
    assert (me["value"], me["passed"], me["decided"], me["teamRate"]) == (50.0, 1, 2, 40.0)  # team: 2 of 5
    assert (team["value"], team["teamRate"], team["rank"], team["of"]) == (40.0, 40.0, None, None)


async def test_normal_user_cannot_see_another_persons_numbers(db):
    ana, ben, boss, _ = await _team()
    assert (await _get(ana, Q + f"&scope=me&user={ben.id}")).status_code == 403
    own = (await _get(ana, Q + f"&scope=me&user={ana.id}")).json()
    assert own["totals"]["rounds"] == 5
    # `user_id` is not the parameter: ignored, so it can never widen what a normal user sees
    assert (await _get(ana, Q + f"&scope=me&user_id={ben.id}")).json()["totals"]["rounds"] == 5
    as_admin = (await _get(boss, Q + f"&scope=me&user={ben.id}")).json()
    assert as_admin["totals"]["rounds"] == 2
    assert [e["name"] for e in as_admin["summary"]["everyone"] if e["isSubject"]] == ["Ben"]
    assert (await _get(boss, Q + f"&scope=me&user={ObjectId()}")).status_code == 404
    mine = (await _get(boss, Q + "&scope=me")).json()  # an admin's own rounds; not ranked among the members
    assert mine["totals"]["rounds"] == 1 and mine["summary"]["cards"]["companies"]["of"] == 3


async def test_period_parameters_and_the_clamp_to_oct_6(db):
    ana = await _user("Ana")
    a = await _acct(ana, "Alpha")
    await _iv(a, ana, ("intro", datetime(2026, 10, 4, 15), "passed"), ("tech_round_1", _d(7), "passed"),
              ("final", datetime(2026, 11, 3, 15), "passed"))
    week = (await _get(ana, "?period=week&date=2026-10-07")).json()
    assert week["window"] == {"start": "2026-10-06", "end": "2026-10-11", "startClamped": True}
    assert (week["totals"]["rounds"], week["totals"]["companies"]) == (1, 0)  # the first held round was Oct 4
    month = (await _get(ana, "?period=month&date=2026-11-15")).json()
    assert month["window"] == {"start": "2026-11-01", "end": "2026-11-30", "startClamped": False}
    assert (month["totals"]["rounds"], month["totals"]["offers"]) == (1, 1)
    before = (await _get(ana, "?period=custom&date=2026-09-01&to=2026-09-30")).json()
    assert before["window"]["startClamped"] is True and before["totals"]["rounds"] == 0
    assert (await _get(ana, "?period=year")).status_code == 422
    assert (await _get(ana, Q + "&scope=profile")).status_code == 422
    assert (await _get(ana, Q + "&region=Mars")).status_code == 422


async def test_bid_side_tables_read_the_applications_row(db):
    ana = await _user("Ana")
    a = await _acct(ana, "Alpha")
    with_row = await _iv(a, ana, ("intro", _d(7), "passed"))
    await _iv(a, ana, ("intro", _d(8), "passed"), appliedBy={"kind": "self"},
              resumeLink={"kind": "tailored", "linkedAt": _d(1), "score": {"total": 70, "band": "good"}})
    await _iv(a, ana, ("intro", _d(9), "passed"))
    await Application.get_motor_collection().insert_one({
        "key": f"{a.id}:x", "accountId": a.id, "ownerId": ana.id, "appliedAt": _d(1), "syncedAt": _d(1),
        "interviewId": with_row, "by": "bidder", "scoreBand": "strong", "area": "Backend"})
    out = (await _get(ana)).json()

    def table(by):
        return {g["key"]: g["rounds"] for g in out[by] if g["rounds"]}

    assert table("byAppliedBy") == {"self": 1, "bidder": 1, "unknown": 1}
    assert table("byScore") == {"strong": 1, "good": 1, "unknown": 1}
    assert table("byArea") == {"Backend": 1, "unknown": 2}
    assert [(g["key"], g["label"]) for g in out["byArea"]][-1] == ("unknown", "Unknown")
    assert [(g["key"], g["label"]) for g in out["byScore"]][-1] == ("unknown", "No score")
    assert all(set(g) == GROUP_KEYS for by in ("byScore", "byArea", "byAppliedBy") for g in out[by])


async def test_contract_details_the_app_relies_on(db):
    ana, ben = await _user("Ana"), await _user("Ben")
    big, small, bens = await _acct(ana, "Big"), await _acct(ana, "Small"), await _acct(ben, "Bens")
    cal = {"enabled": True, "callerName": "Cal", "callerId": "c1"}
    await _many(big, ana, 12)
    await _many(big, ana, 8, "rejected", statusReason="pay")
    await _many(small, ana, 2, "rejected", caller=cal)  # no reason filled in
    await _many(bens, ben, 3)
    await _iv(bens, ben, ("intro", _d(7), "passed"), ("tech_round_1", _d(9), "passed"), ("final", _d(12), "passed"))
    out = (await _get(ana, Q + "&scope=team")).json()
    # 2. a few pass rate is listed, unranked
    everyone = {e["name"]: e for e in out["summary"]["everyone"]}
    assert (everyone["Ben"]["few"], everyone["Ben"]["passRate"], everyone["Ben"]["ranks"]["passRate"]) == (
        True, 100.0, None)
    assert (everyone["Ana"]["few"], everyone["Ana"]["passRate"], everyone["Ana"]["ranks"]["passRate"]) == (
        False, 54.5, 1)
    assert set(everyone["Ana"]) == {"userId", "name", "companies", "held", "decided", "passed", "passRate", "few",
                                    "offers", "isSubject", "ranks"}
    assert set(everyone["Ana"]["ranks"]) == {"companies", "held", "passRate", "offers"}
    # 3. the not-filled reason row is last
    assert [(r["key"], r["label"], r["count"]) for r in out["reasons"]["rejected"]] == [
        ("pay", "Pay or contract terms", 8), ("none", "Reason not filled in yet", 2)]
    # 4. always the three labels, in order
    assert out["depth"]["endedAfter"] == [{"label": "1 round", "count": 10}, {"label": "2 rounds", "count": 0},
                                          {"label": "3 or more", "count": 1}]
    # 6. / 7.
    assert out["summary"]["cards"]["passRate"]["teamRate"] == 64.3
    assert (await _get(ana, Q + "&scope=me")).json()["summary"]["cards"]["passRate"]["teamRate"] == 64.3
    assert out["window"]["startClamped"] is False
    # 8. few profiles and callers fold into one "other" row
    profiles = out["byProfile"]
    assert [(g["key"], g["label"]) for g in profiles] == [(str(big.id), "Big"), ("other", "Other profiles")]
    assert profiles[1]["memberCount"] == 2 and [m["label"] for m in profiles[1]["members"]] == ["Bens", "Small"]
    assert set(profiles[1]) == GROUP_KEYS | {"members", "memberCount"} and set(profiles[0]) == GROUP_KEYS
    assert all(set(m) == GROUP_KEYS for m in profiles[1]["members"])
    assert [(g["key"], g["label"], g["memberCount"]) for g in out["byCaller"]] == [("other", "Other callers", 1)]
    assert out["byCaller"][0]["members"][0]["label"] == "Cal"
    # the finding names the filled reason; marks never land on the folded row or on unknown
    assert {"kind": "reason", "text": "80.0% of rejected rounds: Pay or contract terms (8 of 10)."} in out["findings"]
    assert len(out["findings"]) <= 4
    for by in TABLES:
        marks = [g["mark"] for g in out[by]]
        assert marks.count("best") <= 1 and marks.count("weak") <= 1
        assert all(g["mark"] is None for g in out[by] if g["key"] in ("other", "unknown") or g["few"])


async def test_load_is_cached_per_period_and_drops_big_fields(db, monkeypatch):
    ana = await _user("Ana")
    a = await _acct(ana, "Alpha")
    await _iv(a, ana, ("intro", _d(7), "passed"))
    assert (await _get(ana)).json()["totals"]["rounds"] == 1
    await _iv(a, ana, ("intro", _d(8), "passed"))
    assert (await _get(ana, Q + "&region=EU&scope=me", fresh=False)).json()["totals"]["rounds"] == 1  # one load
    assert (await _get(ana, "?period=custom&date=2026-10-07&to=2026-10-31", fresh=False)).json()["totals"]["rounds"] == 2
    assert (await _get(ana)).json()["totals"]["rounds"] == 2
    seen = []
    real = Interview.model_validate
    monkeypatch.setattr(Interview, "model_validate", lambda d: (seen.append(d), real(d))[1])
    await _get(ana)
    assert seen and all("transcript" not in d and "transcript" not in d["stageHistory"][0] for d in seen)
    assert seen[0]["stageHistory"][0]["status"] == "passed"
