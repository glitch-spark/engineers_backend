"""timing.by_time / bidders (pure) and GET /stats/applications/timing (mongomock)."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")
os.environ["RESUME_QUEUE_DISABLED"] = "1"

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from beanie import init_beanie
from bson import ObjectId
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from mongomock_motor import AsyncMongoMockClient

from app import interview_rates
from app.applications import window as window_mod
from app.deps import get_current_user
from app.models import Account, Application, DailyPlan, Interview, InterviewQuestion, UserExtra
from app.models.bid import Bid
from app.models.bidder import Bidder
from app.models.weekly_plan import WeeklyPlan
from app.routers import stats
from app.stats import profile_loader, timing

pytestmark = [
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]
NY = ZoneInfo("America/New_York")
CUT = datetime(2030, 1, 1)
TUE_0100 = datetime(2026, 3, 3, 1, 0)  # Tuesday 01:00 UTC = Monday 20:00 EST


def _r(at=TUE_0100, iv=False, **kw):
    return {"appliedAt": at, "interviewId": ObjectId() if iv else None, **kw}


def _grp(groups, label):
    return next(g for g in groups if g["label"] == label)


def test_buckets_use_app_timezone():
    out = timing.by_time([_r()], [_r()], CUT, NY)
    assert _grp(out["weekday"], "Mon")["you"]["bids"] == 1 and _grp(out["weekday"], "Tue")["you"]["bids"] == 0
    assert _grp(out["hours"], "18–21")["you"]["bids"] == 1
    late = timing.by_time([_r(datetime(2026, 3, 3, 2, 30))], [], CUT, NY)  # 21:30 EST Monday
    assert _grp(late["hours"], "21–24")["you"]["bids"] == 1


def test_weekday_and_hour_labels():
    out = timing.by_time([], [], CUT, NY)
    assert [g["label"] for g in out["weekday"]] == ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    assert [g["label"] for g in out["hours"]] == ["00–03", "03–06", "06–09", "09–12", "12–15", "15–18", "18–21", "21–24"]


def test_too_few_and_matured_and_rejected():
    rows = [_r(iv=i < 3) for i in range(30)] + [_r(bidStatus="rejected") for _ in range(5)]
    g = _grp(timing.by_time(rows, rows, CUT, NY)["weekday"], "Mon")
    assert g["you"] == {"bids": 30, "matured": 30, "interviews": 3, "per100": 10.0, "tooFew": False}
    g = _grp(timing.by_time(rows[:29], rows, CUT, NY)["weekday"], "Mon")
    assert g["you"]["tooFew"] is True and g["you"]["per100"] is not None
    assert _grp(timing.by_time(rows, rows, TUE_0100 - timedelta(days=1), NY)["weekday"], "Mon")["you"]["matured"] == 0


def test_bidders_rejected_share_and_per100():
    a, b = ObjectId(), ObjectId()
    rows = [_r(bidderId=a, iv=i < 2) for i in range(8)] + [_r(bidderId=a, bidStatus="rejected") for _ in range(2)]
    rows += [_r(bidderId=b)]
    rows += [_r(bidderId=ObjectId()) for _ in range(5)]  # not the subject's bidder
    out = timing.bidders(rows, {a: "Al", b: "Bo"}, CUT)
    assert [x["name"] for x in out] == ["Al", "Bo"]
    assert out[0] == {"bidderId": str(a), "name": "Al", "bids": 8, "matured": 8, "interviews": 2, "per100": 25.0,
                      "tooFew": True, "rejectedShare": 20}
    assert out[1]["rejectedShare"] == 0


def test_team_bidder_baseline():
    ids = [ObjectId() for _ in range(4)]
    rows = []
    for i, p in zip(ids[:3], (3, 6, 9)):  # 30 matured each -> per100 10, 20, 30
        rows += [_r(bidderId=i, iv=k < p) for k in range(30)]
    rows += [_r(bidderId=ids[3]) for _ in range(5)]  # small bidders count too: pooled, not a median of rates
    rows += [_r(bidderId=ids[0], iv=True, bidStatus="rejected"), _r(iv=True)]  # rejected / not a bidder: ignored
    assert timing.team_bidder(rows, CUT) == {"per100": 18.9, "matured": 95, "interviews": 18}
    assert timing.team_bidder(rows, TUE_0100 - timedelta(days=1)) == {"per100": None, "matured": 0, "interviews": 0}
    assert timing.team_bidder([], CUT) == {"per100": None, "matured": 0, "interviews": 0}


def test_no_bidders_empty():
    assert timing.bidders([_r(bidderId=ObjectId())], {}, CUT) == []


# ---- endpoint ----
NOW = datetime.utcnow()
Q = f"?period=custom&date={(NOW - timedelta(days=200)).date()}&to={(NOW + timedelta(days=1)).date()}"


@pytest.fixture(autouse=True)
def _clear_loader_cache():
    profile_loader.clear_cache()
    yield
    profile_loader.clear_cache()


@pytest.fixture
async def db(monkeypatch):
    await init_beanie(database=AsyncMongoMockClient()["stats-timing-unit"], document_models=[
        UserExtra, Account, DailyPlan, Interview, InterviewQuestion, Bid, Bidder, WeeklyPlan, Application])
    window_mod.clear_cache()

    async def regions():
        return {}, {}

    monkeypatch.setattr(interview_rates, "_profile_regions", regions)


def _client(user):
    app = FastAPI()
    app.include_router(stats.router)
    app.dependency_overrides[get_current_user] = lambda: user
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _get(user, qs=Q):
    async with _client(user) as c:
        return await c.get("/stats/applications/timing" + qs)


async def _seed(owner, bidder_id, n, rejected=0, tag=""):
    acct = await Account(name="P" + tag, region="EU", createdBy=owner.id, template="x").insert()
    for i in range(n + rejected):
        await Application(
            key=f"{acct.id}:{i}", accountId=acct.id, ownerId=owner.id, region="EU", bidderId=bidder_id,
            appliedAt=NOW - timedelta(days=60), bidStatus="rejected" if i >= n else None,
            interviewId=ObjectId() if i < 2 else None, syncedAt=NOW).insert()


async def test_timing_endpoint_shape_and_subject_rule(db):
    ana = await UserExtra(name="Ana", email="ana@x.io").insert()
    ben = await UserExtra(name="Ben", email="ben@x.io").insert()
    bid = await Bidder(createdBy=ana.id, name="Al", rate=1.0, screenshotFolderUrl="u").insert()
    bid2 = await Bidder(createdBy=ben.id, name="Bo", rate=1.0, screenshotFolderUrl="u").insert()
    await _seed(ana, bid.id, 30, rejected=10, tag="a")
    await _seed(ben, bid2.id, 30, tag="b")
    out = (await _get(ana)).json()
    assert set(out) == {"label", "subject", "window", "timezone", "weekday", "hours", "bidders", "teamBidder"}
    assert out["subject"] == {"userId": str(ana.id), "name": "Ana"} and out["timezone"]
    assert sum(g["you"]["bids"] for g in out["weekday"]) == 30
    assert sum(g["team"]["bids"] for g in out["weekday"]) == 60
    assert out["bidders"] == [{"bidderId": str(bid.id), "name": "Al", "bids": 30, "matured": 30, "interviews": 2,
                               "per100": 6.7, "tooFew": False, "rejectedShare": 25}]
    assert out["teamBidder"] == {"per100": 6.7, "matured": 60, "interviews": 4}
    assert (await _get(ana, Q + f"&user={ben.id}")).status_code == 403


async def test_timing_no_bidders(db):
    ana = await UserExtra(name="Ana", email="ana@x.io").insert()
    out = (await _get(ana)).json()
    assert out["bidders"] == [] and out["teamBidder"] == {"per100": None, "matured": 0, "interviews": 0}
