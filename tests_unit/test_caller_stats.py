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
    from app.routers import interviews, metrics

    sync = AsyncMock()
    monkeypatch.setattr(interviews, "_sync_caller_posts", sync)

    def make(user):
        app = FastAPI()
        app.include_router(interviews.router)
        app.include_router(metrics.router)
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


async def test_each_callers_interviews_by_the_period_they_were_held_in(db):
    ana = await UserExtra(name="Ana", email="ana@x.io").insert()
    us = await Account(name="Ana US", createdBy=ana.id, region="US").insert()
    eu = await Account(name="Ana EU", createdBy=ana.id, region="EU").insert()
    mu, pi, idle = await _caller("Muhammad"), await _caller("Pietro", "rose"), await _caller("Zed")
    await _interview(ana, us, _round(at(10, 3), "passed", mu), company="Acme")
    await _interview(ana, us, _round(at(10, 6), "rejected", mu), company="Beta")
    await _interview(ana, us, _round(at(10, 20), "completed", mu), company="Cora")  # this period's, awaiting
    await _interview(ana, us, _round(at(9, 28), "completed", mu), company="Dune")  # an earlier period's, awaiting
    await _interview(ana, us, _round(at(9, 15), "passed", mu))  # an earlier period's, decided
    await _interview(ana, us, _round(at(10, 25), "scheduled", mu))  # time passed, still scheduled
    await _interview(ana, us, _round(at(11, 5), "scheduled", mu))  # after the period
    await _interview(ana, us, _round(at(11, 2), "completed", mu))  # held after the period
    await _interview(ana, eu, _round(at(10, 7), "canceled", pi))
    await _interview(ana, eu, _round(at(10, 8), "rejected", typed="Someone"))
    await _interview(ana, us, _round(at(10, 9), "passed", mu, enabled=False))  # no caller requested

    out = await cs.caller_stats("month", date(2026, 10, 1), "all", now=NOW)

    assert out["label"] == "Oct 2026"
    rows = {c["name"]: c for c in out["callers"]}
    assert [c["name"] for c in out["callers"]] == ["Muhammad", "Pietro", "Zed", "Unassigned"]
    assert rows["Muhammad"]["counts"] == {
        "passed": 1, "rejected": 1, "canceled": 0, "awaiting": 1, "outdated": 1, "scheduled": 1}
    assert rows["Muhammad"]["passRate"] == 0.5 and rows["Muhammad"]["color"] == "sky"
    assert rows["Pietro"]["counts"]["canceled"] == 1 and rows["Pietro"]["passRate"] is None
    assert not any(rows["Zed"]["counts"].values())
    assert rows["Unassigned"]["callerId"] is None and rows["Unassigned"]["passRate"] == 0.0

    mine = {(r["company"], r["section"]): r for r in out["rounds"] if r["callerId"] == str(mu.id)}
    assert set(mine) == {("Acme", "period"), ("Beta", "period"), ("Cora", "period"), ("Dune", "outdated"),
                         ("Acme", "scheduled")}
    assert (mine[("Cora", "period")]["result"], mine[("Cora", "period")]["daysWaiting"]) == ("awaiting", 10)
    assert (mine[("Dune", "outdated")]["roundDate"], mine[("Dune", "outdated")]["daysWaiting"]) == (
        "2026-09-28T15:00:00Z", 32)
    assert mine[("Acme", "scheduled")]["stale"]
    assert all(r["engineer"] == "Ana" for r in out["rounds"])
    assert str(idle.id) not in {r["callerId"] for r in out["rounds"]}

    sep = {c["name"]: c for c in (await cs.caller_stats("month", date(2026, 9, 1), "all", now=NOW))["callers"]}
    assert (sep["Muhammad"]["counts"]["passed"], sep["Muhammad"]["counts"]["awaiting"]) == (1, 1)

    eu_only = await cs.caller_stats("month", date(2026, 10, 1), "EU", now=NOW)
    assert {r["callerId"] for r in eu_only["rounds"]} == {str(pi.id), None}


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


async def test_the_endpoint_is_for_caller_managers(client):
    staff = await UserExtra(name="Ana", email="ana@x.io").insert()
    boss = await UserExtra(name="Boss", email="boss@x.io", role="interview_manager").insert()
    async with client(staff) as c:
        assert (await c.get("/metrics/caller-stats")).status_code == 403
    async with client(boss) as c:
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
