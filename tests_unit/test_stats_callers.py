"""Statistics → Callers view and rules (mongomock)."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")
os.environ["RESUME_QUEUE_DISABLED"] = "1"

import json
import re
from datetime import date, datetime

import pytest
from beanie import init_beanie
from mongomock_motor import AsyncMongoMockClient

from app import caller_stats
from app.models import Account, DailyPlan, Interview, InterviewQuestion, UserExtra
from app.models.bid import Bid
from app.models.bidder import Bidder
from app.models.caller import Caller
from app.models.weekly_plan import WeeklyPlan
from app.stats import callers as sc

pytestmark = [
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]

TODAY = date(2026, 11, 18)


# ---- pure rules ----

def test_weak_round_type_fires():
    tip = sc.weak_round_type({"tech": (2, 3), "intro": (4, 1)}, {"tech": (11, 5), "intro": (8, 7)})
    assert tip["severity"] == "bad" and tip["id"] == "callers.weak_round_type" and tip["impact"] > 0
    assert "Tech rounds: 40% pass vs 69% for all callers" in tip["text"] and tip["evidence"]["group"] == "tech"
    assert sc.weak_round_type({"tech": (2, 2)}, {"tech": (11, 5)}) is None  # 4 decided
    assert sc.weak_round_type({"tech": (2, 3)}, {"tech": (2, 2)}) is None  # team thin
    assert sc.weak_round_type({"tech": (3, 2)}, {"tech": (11, 5)}) is None  # 60 vs 69: gap < 15


def test_awaiting_backlog_over_5_after_7_days():
    old = {"result": "awaiting", "day": date(2026, 11, 10)}  # 8 days before
    edge = {"result": "awaiting", "day": date(2026, 11, 11)}  # exactly 7 days: not yet
    assert sc.awaiting_backlog([old] * 5, TODAY) is None
    tip = sc.awaiting_backlog([old] * 6, TODAY)
    assert tip["severity"] == "watch" and tip["text"].startswith("6 rounds are still waiting for a result after a week")
    assert sc.awaiting_backlog([old] * 5 + [edge], TODAY) is None
    assert sc.awaiting_backlog([{"result": "passed", "day": date(2026, 11, 1)}] * 9, TODAY) is None


def test_no_caller_rounds_rule():
    assert sc.no_caller_rounds(5, 0)["text"] == "No caller rounds on your interviews this period"
    assert sc.no_caller_rounds(4, 0) is None and sc.no_caller_rounds(9, 1) is None


# ---- view ----

@pytest.fixture
async def db():
    await init_beanie(
        database=AsyncMongoMockClient()["stats-callers"],
        document_models=[UserExtra, Account, DailyPlan, WeeklyPlan, Interview, InterviewQuestion, Bid, Bidder, Caller],
    )


async def _user(name):
    return await UserExtra(name=name, email=f"{name.lower()}@x.io", callerColor="rose").insert()


async def _round(owner, acc, caller, status, i, stage="intro", day=None):
    """One interview with one round. caller: a Caller/UserExtra (assigned), "TBD" (enabled, unassigned) or None (self)."""
    when = datetime(2026, 11, day or 2 + i % 12, 15)
    c = None
    if caller == "TBD":
        c = {"enabled": True, "callerName": "TBD"}
    elif caller is not None:
        c = {"enabled": True, "callerId": str(caller.id), "callerName": caller.name}
    await Interview(accountId=acc.id, createdBy=owner.id, scheduledAt=when, companyName="Acme", stageHistory=[
        {"stage": stage, "at": when, "scheduledAt": when, "status": status, "caller": c}]).insert()


async def _world():
    """Callers Cal (user; intro 3/2, tech 2/3), Ext1 (3/2, 5/1), Ext2 (2/3, 4/1); engineers Ana, Ben, Cy, Cal (EU)."""
    u = {n: await _user(n) for n in ("Ana", "Ben", "Cy", "Cal")}
    acc = {n: await Account(name=f"{n} EU", createdBy=v.id, region="EU").insert() for n, v in u.items()}
    ext1 = await Caller(name="Ext1", nameKey="ext1", color="sky").insert()
    ext2 = await Caller(name="Ext2", nameKey="ext2", color="sky").insert()
    engs = [u["Ana"], u["Ben"], u["Cy"]]
    i = 0
    for caller, plan in ((u["Cal"], {"intro": "ppprr", "tech_round_1": "pprrr"}),
                         (ext1, {"intro": "ppprr", "tech_round_1": "pppppr"}),
                         (ext2, {"intro": "pprrr", "tech_round_1": "ppppr"})):
        for stage, res in plan.items():
            for ch in res:
                owner = engs[i % 3]
                await _round(owner, acc[owner.name], caller, "passed" if ch == "p" else "rejected", i, stage)
                i += 1
    # Ana / Ben / Cy take rounds themselves: 5 decided each, different rates; Cal too (not enough to matter)
    for owner, res in ((u["Ana"], "prrrr"), (u["Ben"], "ppprp"), (u["Cy"], "ppprr")):
        for ch in res:
            await _round(owner, acc[owner.name], None, "passed" if ch == "p" else "rejected", i)
            i += 1
    return u, acc, ext1, ext2


async def test_mode_caller_when_subject_called(db):
    u, *_ = await _world()
    out = await sc.callers_view(u["Cal"], "month", TODAY, "all", None, TODAY)
    assert out["mode"] == "caller" and out["tab"] == "callers"
    assert [h["key"] for h in out["headline"]] == ["passRate", "rank", "done", "weakest"]
    rate, rank, done, weak = out["headline"]
    assert rate["value"] == 0.5 and rate["note"] == "5 passed · 5 rejected" and rate["team"]["n"] == 3 and rate["rank"] == 3
    assert rank["value"] == 3 and rank["note"] == "of 3"
    assert done["value"] == 10 and done["note"] == "0 awaiting a result" and done["team"]["n"] == 3
    assert weak["value"] == 0.4 and weak["note"] == "Tech" and weak["team"] is None and weak["vsMedian"] is None and weak["rank"] is None
    assert out["everyone"]["rowKind"] == "caller" and out["everyone"]["metric"] == "passRate"
    assert out["notEnoughData"] is False and len(out["definitions"]) == 3
    assert "callers.weak_round_type" in [t["id"] for t in out["tips"]]
    assert out["trend"]["unit"] == "week" and sum(b["you"] for b in out["trend"]["buckets"]) == 10


async def test_mode_engineer_otherwise(db):
    u, *_ = await _world()
    out = await sc.callers_view(u["Ana"], "month", TODAY, "all", None, TODAY)
    assert out["mode"] == "engineer" and out["trend"] is None
    assert [h["key"] for h in out["headline"]] == ["selfRate", "rank", "callerRate", "awaiting"]
    self_, rank, caller, wait = out["headline"]
    assert self_["value"] == 0.2 and self_["format"] == "pct" and self_["team"]["n"] == 3 and self_["rank"] == 3
    assert rank["value"] == 3 and wait["value"] == 0 and wait["format"] == "count" and wait["higherIsBetter"] is False
    assert caller["format"] == "pct" and caller["note"].endswith(" rounds") and caller["team"]["n"] == 3
    assert not any(r["isSubject"] for r in out["everyone"]["rows"])


async def test_caller_and_engineer_uses_caller_numbers_only(db):
    u, acc, ext1, _ = await _world()
    for i in range(6):  # Cal's own interviews: self rounds and rounds another caller took
        await _round(u["Cal"], acc["Cal"], None, "passed", i)
        await _round(u["Cal"], acc["Cal"], ext1, "rejected", i)
    out = await sc.callers_view(u["Cal"], "month", TODAY, "all", None, TODAY)
    assert out["mode"] == "caller" and out["headline"][0]["value"] == 0.5 and out["headline"][2]["value"] == 10
    me = next(r for r in out["everyone"]["rows"] if r["isSubject"])
    assert me["parts"] == {"passed": 5, "rejected": 5}


async def test_under_5_decided_listed_not_ranked_no_weak_tip(db):
    u, acc, *_ = await _world()
    nia = await _user("Nia")
    for i in range(3):
        await _round(u["Ana"], acc["Ana"], nia, "rejected", i, "tech_round_1")
    out = await sc.callers_view(nia, "month", TODAY, "all", None, TODAY)
    assert out["mode"] == "caller" and out["headline"][0]["value"] == 0.0 and out["headline"][0]["rank"] is None
    assert out["headline"][1]["note"] == "needs 5 decided rounds" and out["headline"][3]["value"] is None
    assert "callers.weak_round_type" not in [t["id"] for t in out["tips"]]
    row = next(r for r in out["everyone"]["rows"] if r["isSubject"])
    assert row["rank"] is None and row["vsMedian"] is None and row["value"] == 0.0 and out["headline"][0]["team"]["n"] == 3


async def test_unassigned_in_team_never_subject(db):
    u, acc, *_ = await _world()
    for i in range(6):
        await _round(u["Ana"], acc["Ana"], "TBD", "passed", i)
    out = await sc.callers_view(u["Cal"], "month", TODAY, "all", None, TODAY)
    row = next(r for r in out["everyone"]["rows"] if r["userId"] == "unassigned")
    assert row["rank"] is None and row["vsMedian"] is None and row["isSubject"] is False and row["name"] == "Unassigned"
    assert out["headline"][0]["team"]["n"] == 3 and out["headline"][2]["team"]["n"] == 3  # not in either team
    assert sum(r["isSubject"] for r in out["everyone"]["rows"]) == 1
    ana = await sc.callers_view(u["Ana"], "month", TODAY, "all", None, TODAY)
    assert ana["mode"] == "engineer" and not any(r["isSubject"] for r in ana["everyone"]["rows"])


async def test_region_scopes_both_modes(db):
    u, acc, *_ = await _world()
    us = await Account(name="Ana US", createdBy=u["Ana"].id, region="US").insert()
    for i in range(3):
        await _round(u["Ana"], us, u["Cal"], "passed", i)
    cal = await sc.callers_view(u["Cal"], "month", TODAY, "US", None, TODAY)
    assert cal["mode"] == "caller" and cal["headline"][0]["value"] == 1.0 and cal["headline"][2]["value"] == 3
    assert [r["userId"] for r in cal["everyone"]["rows"]] == [str(u["Cal"].id)]
    ben = await sc.callers_view(u["Ben"], "month", TODAY, "US", None, TODAY)
    assert ben["mode"] == "engineer" and ben["headline"][0]["value"] is None
    ana = await sc.callers_view(u["Ana"], "month", TODAY, "US", None, TODAY)
    assert ana["mode"] == "engineer" and ana["headline"][2]["value"] == 1.0 and ana["headline"][2]["note"] == "3 rounds"
    eu = await sc.callers_view(u["Cal"], "month", TODAY, "EU", None, TODAY)
    assert eu["headline"][0]["value"] == 0.5


async def test_engineer_mode_tips_and_trend_null(db):
    u, acc, ext1, _ = await _world()
    for i in range(6):  # six old awaiting rounds on Ana's interviews
        await _round(u["Ana"], acc["Ana"], ext1, "completed", i, day=2)
    out = await sc.callers_view(u["Ana"], "month", TODAY, "all", None, TODAY)
    assert out["trend"] is None
    assert "callers.awaiting_backlog" in [t["id"] for t in out["tips"]]
    assert out["headline"][3]["value"] == 6


async def test_no_caller_rounds_tip_in_view(db):
    u, acc, *_ = await _world()
    await Interview.find_all().delete()
    for owner, res in ((u["Ana"], "ppprr"), (u["Ben"], "pprrr"), (u["Cy"], "ppppr")):
        for i, ch in enumerate(res):
            await _round(owner, acc[owner.name], None, "passed" if ch == "p" else "rejected", i)
    out = await sc.callers_view(u["Ana"], "month", TODAY, "all", None, TODAY)
    assert "callers.no_caller_rounds" in [t["id"] for t in out["tips"]]


# ---- collect_rounds: caller_stats unchanged ----

# caller_stats output on _world() captured with the pre-refactor code (ObjectIds numbered by first appearance).
GOLDEN = json.loads('''{"callers": [{"byType": {"final": {"done": 0, "passRate": null, "passed": 0, "rejected": 0}, "hiring": {"done": 0, "passRate": null, "passed": 0, "rejected": 0}, "intro": {"done": 0, "passRate": null, "passed": 0, "rejected": 0}, "panel": {"done": 0, "passRate": null, "passed": 0, "rejected": 0}, "tech": {"done": 0, "passRate": null, "passed": 0, "rejected": 0}}, "callerId": "id0", "color": "rose", "counts": {"awaiting": 0, "canceled": 0, "passed": 0, "rejected": 0, "scheduled": 0}, "done": 0, "name": "Ana", "passRate": null, "ranked": false, "vsTeam": null}, {"byType": {"final": {"done": 0, "passRate": null, "passed": 0, "rejected": 0}, "hiring": {"done": 0, "passRate": null, "passed": 0, "rejected": 0}, "intro": {"done": 0, "passRate": null, "passed": 0, "rejected": 0}, "panel": {"done": 0, "passRate": null, "passed": 0, "rejected": 0}, "tech": {"done": 0, "passRate": null, "passed": 0, "rejected": 0}}, "callerId": "id1", "color": "rose", "counts": {"awaiting": 0, "canceled": 0, "passed": 0, "rejected": 0, "scheduled": 0}, "done": 0, "name": "Ben", "passRate": null, "ranked": false, "vsTeam": null}, {"byType": {"final": {"done": 0, "passRate": null, "passed": 0, "rejected": 0}, "hiring": {"done": 0, "passRate": null, "passed": 0, "rejected": 0}, "intro": {"done": 5, "passRate": 0.6, "passed": 3, "rejected": 2}, "panel": {"done": 0, "passRate": null, "passed": 0, "rejected": 0}, "tech": {"done": 5, "passRate": 0.4, "passed": 2, "rejected": 3}}, "callerId": "id2", "color": "rose", "counts": {"awaiting": 0, "canceled": 0, "passed": 5, "rejected": 5, "scheduled": 0}, "done": 10, "name": "Cal", "passRate": 0.5, "ranked": true, "vsTeam": -0.1129}, {"byType": {"final": {"done": 0, "passRate": null, "passed": 0, "rejected": 0}, "hiring": {"done": 0, "passRate": null, "passed": 0, "rejected": 0}, "intro": {"done": 0, "passRate": null, "passed": 0, "rejected": 0}, "panel": {"done": 0, "passRate": null, "passed": 0, "rejected": 0}, "tech": {"done": 0, "passRate": null, "passed": 0, "rejected": 0}}, "callerId": "id3", "color": "rose", "counts": {"awaiting": 0, "canceled": 0, "passed": 0, "rejected": 0, "scheduled": 0}, "done": 0, "name": "Cy", "passRate": null, "ranked": false, "vsTeam": null}, {"byType": {"final": {"done": 0, "passRate": null, "passed": 0, "rejected": 0}, "hiring": {"done": 0, "passRate": null, "passed": 0, "rejected": 0}, "intro": {"done": 5, "passRate": 0.6, "passed": 3, "rejected": 2}, "panel": {"done": 0, "passRate": null, "passed": 0, "rejected": 0}, "tech": {"done": 6, "passRate": 0.8333, "passed": 5, "rejected": 1}}, "callerId": "id4", "color": "sky", "counts": {"awaiting": 0, "canceled": 0, "passed": 8, "rejected": 3, "scheduled": 0}, "done": 11, "name": "Ext1", "passRate": 0.7273, "ranked": true, "vsTeam": 0.1144}, {"byType": {"final": {"done": 0, "passRate": null, "passed": 0, "rejected": 0}, "hiring": {"done": 0, "passRate": null, "passed": 0, "rejected": 0}, "intro": {"done": 5, "passRate": 0.4, "passed": 2, "rejected": 3}, "panel": {"done": 0, "passRate": null, "passed": 0, "rejected": 0}, "tech": {"done": 5, "passRate": 0.8, "passed": 4, "rejected": 1}}, "callerId": "id5", "color": "sky", "counts": {"awaiting": 0, "canceled": 0, "passed": 6, "rejected": 4, "scheduled": 0}, "done": 10, "name": "Ext2", "passRate": 0.6, "ranked": true, "vsTeam": -0.0129}], "end": "2026-11-30", "label": "Nov 2026", "period": "month", "region": "all", "rounds": [{"callerId": "id2", "company": "Acme", "engineer": "Ana", "interviewId": "id6", "result": "passed", "roundDate": "2026-11-02T15:00:00Z", "roundId": "id7", "stage": "intro"}, {"callerId": "id4", "company": "Acme", "engineer": "Ana", "interviewId": "id8", "result": "passed", "roundDate": "2026-11-02T15:00:00Z", "roundId": "id7", "stage": "intro"}, {"callerId": "id5", "company": "Acme", "engineer": "Ana", "interviewId": "id9", "result": "rejected", "roundDate": "2026-11-02T15:00:00Z", "roundId": "id7", "stage": "intro"}, {"callerId": "id2", "company": "Acme", "engineer": "Ben", "interviewId": "id10", "result": "passed", "roundDate": "2026-11-03T15:00:00Z", "roundId": "id11", "stage": "intro"}, {"callerId": "id4", "company": "Acme", "engineer": "Ben", "interviewId": "id12", "result": "rejected", "roundDate": "2026-11-03T15:00:00Z", "roundId": "id11", "stage": "intro"}, {"callerId": "id5", "company": "Acme", "engineer": "Ben", "interviewId": "id13", "result": "rejected", "roundDate": "2026-11-03T15:00:00Z", "roundId": "id11", "stage": "intro"}, {"callerId": "id2", "company": "Acme", "engineer": "Cy", "interviewId": "id14", "result": "passed", "roundDate": "2026-11-04T15:00:00Z", "roundId": "id15", "stage": "intro"}, {"callerId": "id4", "company": "Acme", "engineer": "Cy", "interviewId": "id16", "result": "rejected", "roundDate": "2026-11-04T15:00:00Z", "roundId": "id15", "stage": "intro"}, {"callerId": "id5", "company": "Acme", "engineer": "Cy", "interviewId": "id17", "result": "passed", "roundDate": "2026-11-04T15:00:00Z", "roundId": "id18", "stage": "tech_round_1"}, {"callerId": "id2", "company": "Acme", "engineer": "Ana", "interviewId": "id19", "result": "rejected", "roundDate": "2026-11-05T15:00:00Z", "roundId": "id20", "stage": "intro"}, {"callerId": "id4", "company": "Acme", "engineer": "Ana", "interviewId": "id21", "result": "passed", "roundDate": "2026-11-05T15:00:00Z", "roundId": "id22", "stage": "tech_round_1"}, {"callerId": "id5", "company": "Acme", "engineer": "Ana", "interviewId": "id23", "result": "passed", "roundDate": "2026-11-05T15:00:00Z", "roundId": "id22", "stage": "tech_round_1"}, {"callerId": "id2", "company": "Acme", "engineer": "Ben", "interviewId": "id24", "result": "rejected", "roundDate": "2026-11-06T15:00:00Z", "roundId": "id25", "stage": "intro"}, {"callerId": "id4", "company": "Acme", "engineer": "Ben", "interviewId": "id26", "result": "passed", "roundDate": "2026-11-06T15:00:00Z", "roundId": "id27", "stage": "tech_round_1"}, {"callerId": "id5", "company": "Acme", "engineer": "Ben", "interviewId": "id28", "result": "passed", "roundDate": "2026-11-06T15:00:00Z", "roundId": "id27", "stage": "tech_round_1"}, {"callerId": "id2", "company": "Acme", "engineer": "Cy", "interviewId": "id29", "result": "passed", "roundDate": "2026-11-07T15:00:00Z", "roundId": "id30", "stage": "tech_round_1"}, {"callerId": "id4", "company": "Acme", "engineer": "Cy", "interviewId": "id31", "result": "passed", "roundDate": "2026-11-07T15:00:00Z", "roundId": "id30", "stage": "tech_round_1"}, {"callerId": "id5", "company": "Acme", "engineer": "Cy", "interviewId": "id32", "result": "passed", "roundDate": "2026-11-07T15:00:00Z", "roundId": "id30", "stage": "tech_round_1"}, {"callerId": "id2", "company": "Acme", "engineer": "Ana", "interviewId": "id33", "result": "passed", "roundDate": "2026-11-08T15:00:00Z", "roundId": "id34", "stage": "tech_round_1"}, {"callerId": "id4", "company": "Acme", "engineer": "Ana", "interviewId": "id35", "result": "passed", "roundDate": "2026-11-08T15:00:00Z", "roundId": "id34", "stage": "tech_round_1"}, {"callerId": "id5", "company": "Acme", "engineer": "Ana", "interviewId": "id36", "result": "rejected", "roundDate": "2026-11-08T15:00:00Z", "roundId": "id34", "stage": "tech_round_1"}, {"callerId": "id2", "company": "Acme", "engineer": "Ben", "interviewId": "id37", "result": "rejected", "roundDate": "2026-11-09T15:00:00Z", "roundId": "id38", "stage": "tech_round_1"}, {"callerId": "id4", "company": "Acme", "engineer": "Ben", "interviewId": "id39", "result": "passed", "roundDate": "2026-11-09T15:00:00Z", "roundId": "id38", "stage": "tech_round_1"}, {"callerId": "id2", "company": "Acme", "engineer": "Cy", "interviewId": "id40", "result": "rejected", "roundDate": "2026-11-10T15:00:00Z", "roundId": "id41", "stage": "tech_round_1"}, {"callerId": "id4", "company": "Acme", "engineer": "Cy", "interviewId": "id42", "result": "rejected", "roundDate": "2026-11-10T15:00:00Z", "roundId": "id41", "stage": "tech_round_1"}, {"callerId": "id2", "company": "Acme", "engineer": "Ana", "interviewId": "id43", "result": "rejected", "roundDate": "2026-11-11T15:00:00Z", "roundId": "id44", "stage": "tech_round_1"}, {"callerId": "id5", "company": "Acme", "engineer": "Ana", "interviewId": "id45", "result": "passed", "roundDate": "2026-11-11T15:00:00Z", "roundId": "id46", "stage": "intro"}, {"callerId": "id4", "company": "Acme", "engineer": "Ben", "interviewId": "id47", "result": "passed", "roundDate": "2026-11-12T15:00:00Z", "roundId": "id48", "stage": "intro"}, {"callerId": "id5", "company": "Acme", "engineer": "Ben", "interviewId": "id49", "result": "passed", "roundDate": "2026-11-12T15:00:00Z", "roundId": "id48", "stage": "intro"}, {"callerId": "id4", "company": "Acme", "engineer": "Cy", "interviewId": "id50", "result": "passed", "roundDate": "2026-11-13T15:00:00Z", "roundId": "id51", "stage": "intro"}, {"callerId": "id5", "company": "Acme", "engineer": "Cy", "interviewId": "id52", "result": "rejected", "roundDate": "2026-11-13T15:00:00Z", "roundId": "id51", "stage": "intro"}], "self": {"byType": {"final": {"done": 0, "passRate": null, "passed": 0, "rejected": 0}, "hiring": {"done": 0, "passRate": null, "passed": 0, "rejected": 0}, "intro": {"done": 15, "passRate": 0.5333, "passed": 8, "rejected": 7}, "panel": {"done": 0, "passRate": null, "passed": 0, "rejected": 0}, "tech": {"done": 0, "passRate": null, "passed": 0, "rejected": 0}}, "counts": {"awaiting": 0, "canceled": 0, "passed": 8, "rejected": 7, "scheduled": 0}, "done": 15, "passRate": 0.5333}, "start": "2026-11-01", "team": {"byType": {"final": {"done": 0, "passRate": null, "passed": 0, "rejected": 0}, "hiring": {"done": 0, "passRate": null, "passed": 0, "rejected": 0}, "intro": {"done": 15, "passRate": 0.5333, "passed": 8, "rejected": 7}, "panel": {"done": 0, "passRate": null, "passed": 0, "rejected": 0}, "tech": {"done": 16, "passRate": 0.6875, "passed": 11, "rejected": 5}}, "counts": {"awaiting": 0, "canceled": 0, "passed": 19, "rejected": 12, "scheduled": 0}, "done": 31, "passRate": 0.6129}, "weekly": {"buckets": [{"all": {"done": 0, "passRate": null}, "callers": {}, "end": "2026-11-01", "label": "Nov 1", "start": "2026-11-01"}, {"all": {"done": 21, "passRate": 0.6667}, "callers": {"id2": {"done": 7, "passRate": 0.7143}, "id4": {"done": 7, "passRate": 0.7143}, "id5": {"done": 7, "passRate": 0.5714}}, "end": "2026-11-08", "label": "Nov 2\u20138", "start": "2026-11-02"}, {"all": {"done": 10, "passRate": 0.5}, "callers": {"id2": {"done": 3, "passRate": 0.0}, "id4": {"done": 4, "passRate": 0.75}, "id5": {"done": 3, "passRate": 0.6667}}, "end": "2026-11-15", "label": "Nov 9\u201315", "start": "2026-11-09"}, {"all": {"done": 0, "passRate": null}, "callers": {}, "end": "2026-11-22", "label": "Nov 16\u201322", "start": "2026-11-16"}, {"all": {"done": 0, "passRate": null}, "callers": {}, "end": "2026-11-29", "label": "Nov 23\u201329", "start": "2026-11-23"}, {"all": {"done": 0, "passRate": null}, "callers": {}, "end": "2026-11-30", "label": "Nov 30", "start": "2026-11-30"}], "unit": "week"}}''')


def _jsonable(o):
    return json.loads(json.dumps(o, default=str, sort_keys=True))


async def test_collect_rounds_unchanged_caller_stats(db):
    u, *_ = await _world()
    out = await caller_stats.caller_stats("month", TODAY, "all", None)
    text = json.dumps(_jsonable(out), sort_keys=True)
    seen: dict = {}  # ObjectIds differ per run: number them by first appearance
    text = re.sub(r"[0-9a-f]{24}", lambda m: f"id{seen.setdefault(m.group(0), len(seen))}", text)
    assert json.loads(text) == GOLDEN
