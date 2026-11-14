"""Profiles list: each profile and the owner's profiles together measured the fair way (app.profile_compare) over the
compared window, pass counts over the row's range, and setup units read as the Profile tab reads them
(mongomock-motor)."""
import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")
os.environ["RESUME_QUEUE_DISABLED"] = "1"

import itertools
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

import pytest
from beanie import init_beanie
from mongomock_motor import AsyncMongoMockClient

from app import profile_compare as pc
from app import profile_metrics as pm
from app.config import get_settings
from app.models import (
    Account, Bid, Bidder, DailyPlan, Interview, InterviewQuestion, JobApplicationLog, ResumeApplication, UserExtra,
)

pytestmark = [
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]

_job = itertools.count()
NOV = {"from_": "2026-11-01", "to": "2026-11-13"}


@pytest.fixture
async def db():
    await init_beanie(database=AsyncMongoMockClient()["profiles-list-compare-unit"], document_models=[
        UserExtra, Account, DailyPlan, Interview, InterviewQuestion, Bid, Bidder, JobApplicationLog, ResumeApplication])


@pytest.fixture
def today(monkeypatch):
    from app.routers import accounts

    monkeypatch.setattr(accounts, "_today", lambda: date(2026, 11, 13))
    pc.benchmark_cache.clear()
    yield
    pc.benchmark_cache.clear()


def launch(monkeypatch, day: str):
    monkeypatch.setattr(pm, "get_settings", lambda: get_settings().model_copy(update={"PROFILE_METRICS_SINCE": day}))


async def _user(name, role="staff"):
    return await UserExtra(name=name, email=f"{name}@x.io", role=role).insert()


async def _profile(owner, name, created=datetime(2026, 1, 5, 15), **kw):
    return await Account(name=name, region="US", createdBy=owner.id, createdAt=created, **kw).insert()


async def _bids(owner, acc, day, n):
    b = await Bidder(createdBy=owner.id, name=f"B{next(_job)}", profileId=acc.id, rate=1,
                     screenshotFolderUrl="s3://b/").insert()
    first = datetime(2026, 11, day, 10, tzinfo=ZoneInfo("America/Chicago")).astimezone(timezone.utc).replace(tzinfo=None)
    for _ in range(n):
        await Bid(bidderId=b.id, ownerId=owner.id, jobKey=f"k{next(_job)}", firstAt=first, status="approved",
                  screenshots=[]).insert()


async def _round(owner, acc, day, status="passed"):
    when = datetime(2026, 11, day, 15)
    await Interview(accountId=acc.id, createdBy=owner.id, scheduledAt=when, companyName="Acme",
                    stageHistory=[{"stage": "intro", "at": when, "scheduledAt": when, "status": status}]).insert()


async def _list(user, userId="", **kw):
    from app.routers.accounts import list_profile_metrics

    return await list_profile_metrics(user, userId=userId, **kw)


async def _ana_with_a():
    ana = await _user("ana")
    a = await _profile(ana, "A")
    await _bids(ana, a, 3, 40)
    await _round(ana, a, 4)
    await _round(ana, a, 5, "rejected")
    return ana, a


async def test_rows_carry_compare_rounds_and_name(db, today):
    ana, _a = await _ana_with_a()
    out = await _list(ana, from_="2026-11-01", to="2026-11-30")   # whole month: the window stops at today
    row = out["rows"][0]
    assert row["name"] == "A" and (out["comparedFrom"], out["comparedTo"]) == ("2026-11-01", "2026-11-13")
    assert row["compare"]["days"] == 13
    assert (row["compare"]["bids"], row["compare"]["decided"], row["compare"]["passRate"]) == (40, 2, 0.5)
    assert row["compare"]["interviewsPerBid"] == 0.05
    assert row["rounds"] == {"decided": 2, "passed": 1, "passRate": 0.5}


async def test_mine_pools_listed_profiles_and_drops_idle(db, today):
    bo = await _user("bo")
    b = await _profile(bo, "B")
    await _profile(bo, "C")
    await _bids(bo, b, 3, 60)
    await _round(bo, b, 6)
    out = await _list(bo, **NOV)
    assert (out["mine"]["profiles"], out["mine"]["bids"]) == (1, 60)
    idle = next(r for r in out["rows"] if r["name"] == "C")
    assert idle["compare"]["bids"] == 0          # a row keeps its own numbers even when idle


async def test_window_starts_at_launch_and_creation(db, today, monkeypatch):
    launch(monkeypatch, "2026-11-05")
    ana, _a = await _ana_with_a()
    n = await _profile(ana, "N", created=datetime(2026, 11, 8, 15))
    await _bids(ana, n, 9, 10)
    out = await _list(ana, **NOV)
    assert out["comparedFrom"] == "2026-11-05"
    assert next(r for r in out["rows"] if r["name"] == "N")["compare"]["days"] == 6


async def test_range_before_launch_has_no_compare(db, today, monkeypatch):
    launch(monkeypatch, "2026-11-05")
    ana, _a = await _ana_with_a()
    out = await _list(ana, from_="2026-10-01", to="2026-10-20")
    assert out["comparedFrom"] is None and out["comparedTo"] is None and out["mine"] is None
    assert all(r["compare"] is None and r["rounds"]["decided"] == 0 for r in out["rows"])


async def test_all_time_window_matches_the_benchmark(db, today, monkeypatch):
    from app.routers.accounts import get_metrics_benchmark

    launch(monkeypatch, "2026-11-05")
    ana, _a = await _ana_with_a()
    out = await _list(ana, weeks="all")
    bench = await get_metrics_benchmark(ana, period="all", date="", to="")
    assert out["comparedFrom"] == bench["comparedFrom"] == "2026-11-05"
    assert out["comparedTo"] == bench["end"] == "2026-11-13"
    assert out["rows"][0]["rounds"]["decided"] == 2


async def test_admin_viewing_everyone_gets_no_compare(db, today):
    ana, _a = await _ana_with_a()
    admin = await _user("root", role="admin")
    out = await _list(admin, **NOV)
    assert out["mine"] is None and all(r["compare"] is None for r in out["rows"])
    assert all("rounds" in r and "name" in r for r in out["rows"])
    picked = await _list(admin, userId=str(ana.id), **NOV)
    assert picked["mine"]["bids"] == 40


# ---- setup units on the profiles list ---------------------------------------------------------------------------------

async def _accounts(user):
    from app.routers.accounts import list_accounts

    return await list_accounts(user, page=1, limit=10, search="", userId="", status_="active", region="")


async def test_list_setup_units_follow_the_profile_tab(db, today):
    from app.models.account import ResumePrompt

    ana = await _user("ana")
    await _profile(ana, "P", defaultPromptId="u1", resumePrompts=[
        ResumePrompt(id="u1", name="Backend", roles=["backend"], body="Rewrite", templateHtml="<p>x</p>"),
        ResumePrompt(id="u2", name="QA", roles=["qa"], body="  ", templateHtml=""),
    ])
    p = (await _accounts(ana))["accounts"][0]
    assert [(u["id"], u["hasTemplate"], u["hasPrompt"]) for u in p["setupUnits"]] == [("u1", True, True), ("u2", False, False)]
    assert p["setupUnits"][0]["roles"] == ["backend"] and p["setupDefaultId"] == "u1"
    assert all("body" not in u and "templateHtml" not in u for u in p["setupUnits"])


async def test_list_setup_units_for_older_profiles(db, today):
    from app.models.account import ResumePrompt

    ana = await _user("ana")
    await _profile(ana, "L", resumePromptBody="# Backend\nRewrite", styleTemplate="<p/>")
    await _profile(ana, "M", styleTemplate="<p/>", resumePrompts=[
        ResumePrompt(id="m1", name="Backend", roles=["backend"], body="Rewrite", templateHtml=None)])
    by = {a["name"]: a for a in (await _accounts(ana))["accounts"]}
    assert [(u["hasTemplate"], u["hasPrompt"]) for u in by["L"]["setupUnits"]] == [(True, True)]
    assert by["M"]["setupUnits"][0]["hasTemplate"] is True


async def test_list_reads_interview_rounds_once(db, today, monkeypatch):
    from app.routers import accounts

    ana, _a = await _ana_with_a()
    calls = []
    real = accounts.profile_rounds

    async def counting(ids, start, end):
        calls.append((start, end))
        return await real(ids, start, end)

    monkeypatch.setattr(accounts, "profile_rounds", counting)
    out = await _list(ana, **NOV)
    assert len(calls) == 1
    assert out["rows"][0]["rounds"]["decided"] == 2 and out["rows"][0]["compare"]["decided"] == 2
