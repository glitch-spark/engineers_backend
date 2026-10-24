"""Statistics → Interview rate (in-memory MongoDB via mongomock-motor): applies from followed-up daily plans,
interview rounds held, and the rate, per user for one week or month, in a region or all of them."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")
os.environ["RESUME_QUEUE_DISABLED"] = "1"

from datetime import date, datetime

import pytest
from beanie import init_beanie
from mongomock_motor import AsyncMongoMockClient

from app import interview_rates as ir
from app.models import Account, DailyPlan, Interview, InterviewQuestion, UserExtra
from app.routers.metrics import interview_rates as endpoint

pytestmark = [  # pre-existing utcnow defaults (app/models/base.py) and Beanie internals
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]

DAY = date(2026, 10, 21)  # a Wednesday: week Oct 19–25
IN_WEEK = datetime(2026, 10, 20, 15)  # 15:00 UTC: Oct 20 in any app time zone from UTC-12 to UTC+8


@pytest.fixture
async def db():
    database = AsyncMongoMockClient()["interview-rates-unit"]
    await init_beanie(database=database, document_models=[UserExtra, Account, DailyPlan, Interview, InterviewQuestion])


async def _plan(user, day: date, logged=True, **done):
    """A day's plan with these applies Done (self per region, a plain self total, or bidder)."""
    await DailyPlan(
        userId=user.id, date=datetime(day.year, day.month, day.day), year=day.year, weekNumber=43, done=done,
        loggedAt=datetime(day.year, day.month, day.day, 20) if logged else None,
    ).insert()


async def _interview(user, acc, *rounds):
    """rounds: (stage, when, status)."""
    history = [{"stage": s, "at": w, "scheduledAt": w, "status": st} for s, w, st in rounds]
    await Interview(
        accountId=acc.id, createdBy=user.id, scheduledAt=rounds[-1][1], companyName="Acme", stageHistory=history,
    ).insert()


def test_week_and_month_windows():
    assert ir.window("week", DAY) == (date(2026, 10, 19), date(2026, 10, 25))
    assert ir.window("month", date(2026, 2, 10)) == (date(2026, 2, 1), date(2026, 2, 28))
    assert ir.label("week", *ir.window("week", DAY)) == "Oct 19–25, 2026"
    assert ir.label("week", *ir.window("week", date(2026, 10, 1))) == "Sep 28 – Oct 4, 2026"


async def test_applies_come_from_followed_up_daily_plans_by_region(db):
    ana = await UserExtra(name="Ana", email="ana@x.io").insert()
    latam = await Account(name="Ana LATAM", createdBy=ana.id, region="Latam").insert()
    eu = await Account(name="Ana EU", createdBy=ana.id, region="EU").insert()
    await _plan(ana, date(2026, 10, 19), bidsSelfLatam=30, bidsSelfEu=8, bidsSelf=38, bidsBidder=40)
    await _plan(ana, date(2026, 10, 20), bidsSelfLatam=10, bidsSelfEu=2, bidsSelf=12)
    await _plan(ana, date(2026, 10, 21), bidsSelf=5)  # a plain total: no region
    await _plan(ana, date(2026, 10, 22), logged=False, bidsSelfLatam=99, bidsSelf=99)  # goal only: not followed up
    await _plan(ana, date(2026, 10, 12), bidsSelfLatam=7, bidsSelf=7)  # the week before
    await _interview(ana, latam, ("intro", datetime(2026, 10, 13), "passed"), ("tech_round_1", IN_WEEK, "scheduled"))
    await _interview(ana, eu, ("intro", IN_WEEK, "scheduled"))
    await _interview(ana, eu, ("intro", IN_WEEK, "canceled"))  # canceled rounds don't count

    def row(out):
        return {k: out["rows"][0][k] for k in ("bids", "interviews", "rate", "lowVolume")}

    assert row(await endpoint(ana, period="week", date="2026-10-21", region="all")) == {  # self 55 + bidder 40
        "bids": 95, "interviews": 2, "rate": round(2 / 95, 4), "lowVolume": False}
    assert row(await endpoint(ana, period="week", date="2026-10-21", region="Latam")) == {
        "bids": 40, "interviews": 1, "rate": 0.025, "lowVolume": False}
    assert row(await endpoint(ana, period="week", date="2026-10-21", region="EU")) == {
        "bids": 10, "interviews": 1, "rate": 0.1, "lowVolume": True}
    out = await endpoint(ana, period="week", date="2026-10-21", region="all")
    assert (out["rows"][0]["selfBids"], out["rows"][0]["bidderBids"], out["bidderSplit"]) == (55, 40, True)
    assert (out["totals"]["selfBids"], out["totals"]["bidderBids"]) == (55, 40)
    out = await endpoint(ana, period="week", date="2026-10-21", region="Latam")
    assert (out["rows"][0]["selfBids"], out["rows"][0]["bidderBids"], out["bidderSplit"]) == (40, 0, False)
    out = await endpoint(ana, period="week", date="2026-10-21", region="US")  # bidders apply to US jobs only
    assert row(out) == {"bids": 40, "interviews": 0, "rate": 0.0, "lowVolume": False}
    assert (out["rows"][0]["selfBids"], out["rows"][0]["bidderBids"], out["bidderSplit"]) == (0, 40, True)


async def test_everyone_is_ranked_by_rate_low_volume_last(db):
    ana, ben, cy, dee = [await UserExtra(name=n, email=f"{n}@x.io").insert() for n in ("Ana", "Ben", "Cy", "Dee")]
    boss = await UserExtra(name="Boss", email="boss@x.io", role="admin").insert()
    await UserExtra(name="Idle", email="idle@x.io").insert()
    accs = {u.name: await Account(name=u.name, createdBy=u.id, region="US").insert() for u in (ana, ben, cy, dee)}
    await _plan(ana, DAY, bidsSelfUs=40, bidsSelf=40)
    await _plan(ben, DAY, bidsSelfUs=50, bidsSelf=50)
    await _plan(cy, DAY, bidsSelfUs=5, bidsSelf=5)  # below MIN_BIDS
    await _plan(boss, DAY, bidsSelf=40)  # admins aren't on the board
    for _ in range(2):
        await _interview(ana, accs["Ana"], ("intro", IN_WEEK, "scheduled"))
    for _ in range(4):
        await _interview(ben, accs["Ben"], ("intro", IN_WEEK, "scheduled"))
    for _ in range(3):
        await _interview(cy, accs["Cy"], ("intro", IN_WEEK, "scheduled"))

    out = await endpoint(dee, period="week", date="2026-10-21", region="all")

    assert [(r["name"], r["rank"], r["isMe"]) for r in out["rows"]] == [
        ("Ben", 1, False), ("Ana", 2, False), ("Cy", None, False), ("Dee", None, True)]
    assert "Idle" not in {r["name"] for r in out["rows"]}
    assert out["rows"][-1]["bids"] == 0  # the viewer is listed even with nothing
    assert out["totals"] == {"bids": 95, "selfBids": 95, "bidderBids": 0, "interviews": 9, "rate": round(9 / 95, 4)}
    assert out["label"] == "Oct 19–25, 2026" and out["minBids"] == ir.MIN_BIDS


async def test_trend_has_the_viewers_and_teams_rate_per_period(db):
    ana = await UserExtra(name="Ana", email="ana@x.io").insert()
    ben = await UserExtra(name="Ben", email="ben@x.io").insert()
    b_acc = await Account(name="Ben", createdBy=ben.id, region="US").insert()
    await _plan(ana, date(2026, 9, 10), bidsSelf=10)
    await _plan(ben, date(2026, 9, 12), bidsSelf=10, bidsBidder=20)
    await _interview(ben, b_acc, ("intro", datetime(2026, 9, 20, 15), "scheduled"))
    await _plan(ana, date(2026, 10, 5), bidsSelf=20)

    out = await endpoint(ana, period="month", date="2026-10-21", region="all")

    trend = out["trend"]
    assert len(trend) == ir.TREND_PERIODS and [t["label"] for t in trend[-2:]] == ["Sep 2026", "Oct 2026"]
    sep, oct_ = trend[-2], trend[-1]
    assert (sep["bids"], sep["rate"], sep["teamBids"], sep["teamInterviews"]) == (10, 0.0, 40, 1)
    assert sep["teamRate"] == 0.025
    assert (oct_["bids"], oct_["teamBids"], oct_["teamRate"]) == (20, 20, 0.0)
    assert trend[0]["start"] == "2026-03-01"
