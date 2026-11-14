"""Statistics → Callers (in-memory MongoDB via mongomock-motor): each caller's rounds held in a period, outdated
pending and scheduled rounds, Mark rejected, and linking typed caller names."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")
os.environ["RESUME_QUEUE_DISABLED"] = "1"

from datetime import date, datetime, timezone
from unittest.mock import AsyncMock

import pytest
from beanie import init_beanie
from mongomock_motor import AsyncMongoMockClient

from app import caller_stats as cs
from app.models import Account, Interview, InterviewQuestion, UserExtra
from app.models.caller import Caller
from scripts.link_typed_callers import plan_links

pytestmark = [  # pre-existing utcnow defaults (app/models/base.py) and Beanie internals
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]

NOW = datetime(2026, 10, 30, 12, tzinfo=timezone.utc)


def at(month, day, hour=15):
    """15:00 UTC: the same local day in any app time zone from UTC-12 to UTC+8."""
    return datetime(2026, month, day, hour)


@pytest.fixture
async def db():
    database = AsyncMongoMockClient()["caller-stats-unit"]
    await init_beanie(database=database, document_models=[UserExtra, Account, Interview, InterviewQuestion, Caller])
    return database


@pytest.fixture
def client(db, monkeypatch):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.deps import get_current_user
    from app.routers import callers, interviews, metrics

    sync = AsyncMock()
    monkeypatch.setattr(interviews, "_sync_caller_posts", sync)

    def make(user):
        app = FastAPI()
        app.include_router(interviews.router)
        app.include_router(metrics.router)
        app.include_router(callers.router)
        app.dependency_overrides[get_current_user] = lambda: user
        c = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
        c.sync = sync
        return c

    return make


async def _caller(name, color="sky"):
    return await Caller(name=name, nameKey=name.lower(), color=color).insert()


def _round(when, status, caller=None, typed="TBD", enabled=True, stage="intro"):
    return {"stage": stage, "at": when, "scheduledAt": when, "status": status,
            "caller": {"enabled": enabled, "callerId": str(caller.id) if caller else None,
                       "callerName": caller.name if caller else typed}}


async def _interview(owner, acc, *rounds, company="Acme"):
    return await Interview(
        accountId=acc.id, createdBy=owner.id, scheduledAt=rounds[-1]["scheduledAt"], companyName=company,
        stageHistory=list(rounds),
    ).insert()


async def _october():
    """Ana (US). Muhammad: intro passed Oct 3, tech rejected Oct 6, intro awaiting Oct 20, intro canceled Oct 8, intro
    still scheduled Oct 25 (date passed), a home assessment Oct 9 (not counted), intro passed Sep 15 (another period).
    Pietro: intro passed Oct 2, 5, 13 and (completed then passed, one round) Oct 12; final rejected Oct 14.
    Self rounds: intro passed Oct 10, live coding rejected Oct 11."""
    ana = await UserExtra(name="Ana", email="ana@x.io").insert()
    us = await Account(name="Ana US", createdBy=ana.id, region="US").insert()
    mu, pi = await _caller("Muhammad"), await _caller("Pietro", "rose")
    await _interview(ana, us, _round(at(10, 3), "passed", mu), company="Acme")
    await _interview(ana, us, _round(at(10, 6), "rejected", mu, stage="tech_round_1"), company="Beta")
    await _interview(ana, us, _round(at(10, 20), "completed", mu), company="Cora")
    await _interview(ana, us, _round(at(10, 8), "canceled", mu), company="Dune")
    await _interview(ana, us, _round(at(10, 25), "scheduled", mu), company="Echo")
    await _interview(ana, us, _round(at(10, 9), "passed", mu, stage="home_assessment"), company="Fizz")
    await _interview(ana, us, _round(at(9, 15), "passed", mu), company="Gone")
    for d in (2, 5, 13):
        await _interview(ana, us, _round(at(10, d), "passed", pi), company=f"P{d}")
    await _interview(ana, us, _round(at(10, 12), "completed", pi), _round(at(10, 12, 16), "passed", pi), company="Twice")
    await _interview(ana, us, _round(at(10, 14), "rejected", pi, stage="final"), company="Fin")
    await _interview(ana, us, _round(at(10, 10), "passed", enabled=False), company="SelfA")
    await _interview(ana, us, _round(at(10, 11), "rejected", enabled=False, stage="live_coding"), company="SelfB")
    return mu, pi


async def test_callers_done_pass_rate_and_ranking(db):
    await _october()
    out = await cs.caller_stats("month", date(2026, 10, 1), "all", now=NOW)
    assert out["label"] == "Oct 2026"
    rows = {c["name"]: c for c in out["callers"]}
    assert rows["Muhammad"]["counts"] == {"passed": 1, "rejected": 1, "awaiting": 1, "canceled": 1, "scheduled": 1}
    assert (rows["Muhammad"]["done"], rows["Muhammad"]["passRate"], rows["Muhammad"]["ranked"]) == (3, 0.5, False)
    assert (rows["Pietro"]["done"], rows["Pietro"]["passRate"], rows["Pietro"]["ranked"]) == (5, 0.8, True)
    assert out["team"]["passRate"] == 0.7143 and out["team"]["done"] == 8
    assert (rows["Pietro"]["vsTeam"], rows["Muhammad"]["vsTeam"]) == (0.0857, -0.2143)
    assert "outdated" not in rows["Muhammad"]["counts"]
    assert rows["Muhammad"]["color"] == "sky"


async def test_pass_rate_by_round_type_and_self(db):
    await _october()
    out = await cs.caller_stats("month", date(2026, 10, 1), "all", now=NOW)
    mu = next(c for c in out["callers"] if c["name"] == "Muhammad")
    assert mu["byType"]["tech"] == {"done": 1, "passed": 0, "rejected": 1, "passRate": 0.0}
    assert mu["byType"]["final"] == {"done": 0, "passed": 0, "rejected": 0, "passRate": None}
    assert set(mu["byType"]) == {"intro", "tech", "hiring", "panel", "final"}
    assert (out["self"]["done"], out["self"]["passRate"]) == (2, 0.5)
    assert out["self"]["byType"]["tech"]["passRate"] == 0.0 and out["self"]["byType"]["intro"]["passRate"] == 1.0
    assert out["team"]["byType"]["final"] == {"done": 1, "passed": 0, "rejected": 1, "passRate": 0.0}


async def test_weekly_buckets_add_up(db):
    mu, pi = await _october()
    out = await cs.caller_stats("month", date(2026, 10, 1), "all", now=NOW)
    assert out["weekly"]["unit"] == "week"
    assert sum(b["callers"].get(str(mu.id), {"done": 0})["done"] for b in out["weekly"]["buckets"]) == 3
    assert sum(b["callers"].get(str(pi.id), {"done": 0})["done"] for b in out["weekly"]["buckets"]) == 5
    assert sum(b["all"]["done"] for b in out["weekly"]["buckets"]) == out["team"]["done"]


async def test_rounds_list_only_rounds_held_in_the_period(db):
    mu, pi = await _october()
    out = await cs.caller_stats("month", date(2026, 10, 1), "all", now=NOW)
    mine = [r for r in out["rounds"] if r["callerId"] == str(mu.id)]
    assert sorted(r["result"] for r in mine) == ["awaiting", "canceled", "passed", "rejected"]
    assert all("section" not in r and "stale" not in r for r in out["rounds"])
    assert sum(1 for r in out["rounds"] if r["callerId"] == str(pi.id)) == 5  # Twice is one round
    assert all(r["engineer"] == "Ana" for r in out["rounds"])


async def test_region_without_caller_rounds_has_no_rates(db):
    await _october()
    out = await cs.caller_stats("month", date(2026, 10, 1), "EU", now=NOW)
    assert all(c["done"] == 0 for c in out["callers"]) and out["team"]["passRate"] is None
    assert out["self"]["done"] == 0 and out["rounds"] == []


async def test_a_typed_caller_name_counts_for_that_caller(db):
    """Rounds saved before the caller list have only a typed name: it counts for the caller of that name (any case,
    or a known other spelling); TBD and unknown names stay Unassigned."""
    ana = await UserExtra(name="Ana", email="ana@x.io").insert()
    us = await Account(name="Ana US", createdBy=ana.id, region="US").insert()
    mu = await _caller("Muhammad")
    await _interview(ana, us, _round(at(10, 5), "canceled", typed="Mohammad"), company="Zoox")
    await _interview(ana, us, _round(at(10, 6), "rejected", typed=" muhammad "), company="Recuro")
    await _interview(ana, us, _round(at(10, 7), "canceled", typed="TBD"), company="Open")
    await _interview(ana, us, _round(at(10, 8), "passed", typed="David"), company="Dave")

    out = await cs.caller_stats("month", date(2026, 10, 1), "all", now=NOW)

    rows = {c["name"]: c for c in out["callers"]}
    assert (rows["Muhammad"]["counts"]["canceled"], rows["Muhammad"]["counts"]["rejected"]) == (1, 1)
    assert {r["company"] for r in out["rounds"] if r["callerId"] == str(mu.id)} == {"Zoox", "Recuro"}
    assert {r["company"] for r in out["rounds"] if r["callerId"] is None} == {"Open", "Dave"}


async def test_every_signed_in_user_sees_the_report(client):
    staff = await UserExtra(name="Ana", email="ana@x.io").insert()
    async with client(staff) as c:
        assert (await c.get("/metrics/caller-stats", params={"period": "month", "date": "2026-10-01"})).status_code == 200
        bad = await c.get("/metrics/caller-stats", params={"period": "custom", "date": "2026-10-09", "to": "2026-10-01"})
        assert bad.status_code == 400


async def test_mark_rejected_closes_an_awaiting_round(client):
    ana = await UserExtra(name="Ana", email="ana@x.io").insert()
    boss = await UserExtra(name="Boss", email="boss@x.io", role="interview_manager").insert()
    acc = await Account(name="Ana US", createdBy=ana.id, region="US").insert()
    mu = await _caller("Muhammad")
    iv = await _interview(ana, acc, _round(at(10, 3), "passed", mu), _round(at(10, 9), "completed", mu, stage="final"))
    old, held = (await Interview.get(iv.id)).stageHistory

    async with client(ana) as c:  # the owner isn't a caller manager
        assert (await c.post(f"/interviews/{iv.id}/stages/{held.id}/reject")).status_code == 403
    async with client(boss) as c:
        assert (await c.post(f"/interviews/{iv.id}/stages/{old.id}/reject")).status_code == 409  # already decided
        assert (await c.post(f"/interviews/{iv.id}/stages/nope/reject")).status_code == 404
        r = await c.post(f"/interviews/{iv.id}/stages/{held.id}/reject")
        assert r.status_code == 200
        c.sync.assert_awaited_once()

    saved = await Interview.get(iv.id)
    assert getattr(saved.status, "value", saved.status) == "rejected"  # the interview mirrors its latest round
    old, held = saved.stageHistory
    assert getattr(held.status, "value", held.status) == "rejected"
    assert getattr(old.status, "value", old.status) == "passed"


def test_typed_caller_names_link_by_name_and_alias():
    mu = {"_id": "m1", "name": "Muhammad", "nameKey": "muhammad"}
    pi = {"_id": "p1", "name": "Pietro", "nameKey": "pietro"}
    callers = {"muhammad": mu, "pietro": pi}

    def r(name, **kw):
        return {"caller": {"enabled": True, "callerName": name, **kw}}

    doc = {"stageHistory": [r(" pietro "), r("Mohammad"), r("TBD"), r("David"), r("Muhammad", callerId="m1"),
                            {"caller": {"enabled": False, "callerName": "Pietro"}}]}
    assert [(i, c["name"]) for i, _, c in plan_links(doc, callers)] == [(0, "Pietro"), (1, "Muhammad")]


async def test_rows_include_platform_callers(db):
    """A round assigned to a platform user gets a row under the user's id, name and directory colour."""
    from app.caller_directory import user_caller_color

    ana = await UserExtra(name="Ana", email="ana@x.io", callerColor="rose").insert()
    owner = await UserExtra(name="Owner", email="owner@x.io").insert()
    us = await Account(name="Owner US", createdBy=owner.id, region="US").insert()
    await _interview(owner, us, _round(at(10, 5), "passed", ana), company="Acme")

    out = await cs.caller_stats("month", date(2026, 10, 1), "all", now=NOW)

    row = next(c for c in out["callers"] if c["callerId"] == str(ana.id))
    assert (row["name"], row["color"]) == ("Ana", user_caller_color(str(ana.id), "rose")) and row["color"] == "rose"
    assert row["counts"]["passed"] == 1
    assert not any(c["name"] == "Removed caller" for c in out["callers"])


async def test_rows_include_archived_external_callers(db):
    from datetime import datetime as dt

    ex = await _caller("Gone", "rose")
    ex.archivedAt = dt(2026, 9, 1)
    await ex.save()
    out = await cs.caller_stats("month", date(2026, 10, 1), "all", now=NOW)
    assert [(c["name"], c["color"]) for c in out["callers"] if c["callerId"] == str(ex.id)] == [("Gone", "rose")]


async def test_overview_counts(db):
    """NOW is Fri 2026-10-30: one upcoming scheduled round, passed + rejected in the last 90 days, one passed 120
    days ago; the rejected one is in this week (Oct 26 - Nov 1)."""
    ana = await UserExtra(name="Ana", email="ana@x.io").insert()
    us = await Account(name="Ana US", createdBy=ana.id, region="US").insert()
    mu = await _caller("Muhammad")
    await _interview(ana, us, _round(at(11, 5), "scheduled", mu), company="Up")
    await _interview(ana, us, _round(at(10, 20), "passed", mu), company="Pass")
    await _interview(ana, us, _round(at(10, 28), "rejected", mu), company="Rej")
    await _interview(ana, us, _round(datetime(2026, 7, 2, 15), "passed", mu), company="Old")
    await _interview(ana, us, _round(at(10, 29), "passed", mu, stage="home_assessment"), company="HA")
    await _interview(ana, us, _round(at(10, 29), "passed", enabled=False), company="SelfRound")

    out = await cs.caller_overview(NOW)

    assert out[str(mu.id)] == {"upcoming": 1, "thisWeek": 1, "passRate": 0.5, "rounds": 4}


async def test_overview_endpoint(client):
    staff = await UserExtra(name="Ana", email="ana@x.io").insert()
    async with client(staff) as c:
        r = await c.get("/callers/overview")
    assert r.status_code == 200 and r.json() == {"overview": {}}
