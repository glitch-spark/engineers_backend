"""Statistics → AI usage view and rules (mongomock)."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")
os.environ["RESUME_QUEUE_DISABLED"] = "1"

from datetime import date, datetime

import pytest
from beanie import init_beanie
from mongomock_motor import AsyncMongoMockClient

from app.models import AiUsageEvent, UserExtra
from app.stats import ai_usage as sa

pytestmark = [
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]

TODAY = date(2026, 11, 18)


@pytest.fixture
async def db():
    from app.models import DailyPlan, Interview, InterviewQuestion, Account

    await init_beanie(database=AsyncMongoMockClient()["stats-ai-usage"],
                      document_models=[UserExtra, AiUsageEvent, DailyPlan, Interview, InterviewQuestion, Account])


async def _user(name, role="staff"):
    return await UserExtra(name=name, email=f"{name.lower()}@x.io", role=role).insert()


async def _ev(user, cost, feature="resume", model="m1", ref=None, ok=True, calls=1, d=5, m=11):
    e = AiUsageEvent(userId=user.id, feature=feature, model=model, costUsd=cost, refId=ref, ok=ok, calls=calls,
                     createdAt=datetime(2026, m, d, 12))
    await e.insert()


async def _team(costs=(1.0, 2.0, 3.0, 4.0)):
    users = [await _user(n) for n in ("Ana", "Ben", "Cy", "Dee")]
    for u, c in zip(users, costs):
        await _ev(u, c, feature="chat")
    return users


async def _view(u, period="month"):
    return await sa.ai_usage_view(u, period, TODAY, "all", None, TODAY)


def test_expensive_model_tip():
    tip = sa.expensive_model(1.0, 0.4, "big", ("small", 0.3))
    assert tip["severity"] == "watch" and "$1.00" in tip["text"] and "$0.40" in tip["text"] and "small" in tip["text"]
    assert sa.expensive_model(0.7, 0.4, "big", ("small", 0.3)) is None  # < 2x median
    assert sa.expensive_model(1.0, 0.4, "small", ("small", 0.3)) is None  # same model
    assert sa.expensive_model(1.0, 0.4, "big", ("pricey", 1.5)) is None  # not cheaper
    assert sa.expensive_model(None, 0.4, "big", ("small", 0.3)) is None


async def test_cost_and_unpriced(db):
    ana, *_ = await _team()
    await _ev(ana, None, feature="chat", calls=2)
    await _ev(ana, 0.5, feature="chat", d=6, m=12)  # outside the period
    out = await _view(ana)
    h = out["headline"][0]
    assert h["value"] == 1.0 and h["format"] == "usd" and h["higherIsBetter"] is False
    assert h["note"] == "3 calls · 2 unpriced"


async def test_cost_per_resume_distinct_refids_ok_only(db):
    ana, *_ = await _team()
    await _ev(ana, 0.4, ref="j1")
    await _ev(ana, 0.2, ref="j1")  # retry of the same job
    await _ev(ana, 0.4, ref="j2")
    await _ev(ana, 0.4, ref="j3", ok=False)  # failed: spend counts, resume does not
    await _ev(ana, 0.4, ref=None)
    out = await _view(ana)
    per = out["headline"][2]
    assert per["note"] == "2 resumes" and per["value"] == pytest.approx(1.8 / 2)
    row = next(r for r in out["everyone"]["rows"] if r["isSubject"])
    assert row["parts"]["resume"] == pytest.approx(1.8) and row["parts"]["other"] == pytest.approx(1.0)


async def test_cost_per_interview_none_without_interviews(db):
    ana, *_ = await _team()
    out = await _view(ana)
    h = out["headline"][3]
    assert h["key"] == "perInterview" and h["value"] is None and h["note"] == "0 interviews"


async def test_rank_cheapest_first(db):
    ana, ben, cy, dee = await _team()
    out = await _view(dee)
    assert out["headline"][0]["rank"] == 4 and out["headline"][1]["value"] == 4
    names = [r["name"] for r in out["everyone"]["rows"]]
    assert names == ["Ana", "Ben", "Cy", "Dee"] and out["everyone"]["rowKind"] == "person"
    assert out["everyone"]["metric"] == "cost"
    zed = await _user("Zed")
    out = await _view(zed)
    h = out["headline"][0]
    assert h["value"] == 0 and h["rank"] == 1 and h["of"] == 5 and h["team"]["n"] == 4 and out["tips"] == []


async def test_expensive_model_tip_in_view(db):
    ana, ben, cy, dee = await _team((0, 0, 0, 0))
    for i, u in enumerate((ben, cy, dee)):
        for j in range(2):
            await _ev(u, 0.2, model="small", ref=f"{i}{j}")
    for j in range(2):
        await _ev(ana, 1.0, model="big", ref=f"a{j}")
    out = await _view(ana)
    assert [t["id"] for t in out["tips"]] == ["ai_usage.expensive_model"]


async def test_contract_keys(db):
    ana, *_ = await _team()
    out = await _view(ana)
    assert out["tab"] == "ai-usage" and out["notEnoughData"] is False and len(out["definitions"]) == 3
    assert [h["key"] for h in out["headline"]] == ["cost", "rank", "perResume", "perInterview"]
    assert set(out) >= {"label", "subject", "headline", "tips", "everyone", "trend", "notEnoughData", "definitions"}
    assert out["trend"]["buckets"]
