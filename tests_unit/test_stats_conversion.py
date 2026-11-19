"""GET /stats/conversion and conversion.report (mongomock)."""

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

from app import interview_rates
from app.deps import get_current_user
from app.models import Account, Application, DailyPlan, Interview, InterviewQuestion, UserExtra
from app.models.bid import Bid
from app.models.bidder import Bidder
from app.models.weekly_plan import WeeklyPlan
from app.routers import stats
from app.stats import conversion, profile_loader

pytestmark = [
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]
WIN = {"median": 5, "p90": 14, "matured_days": 14, "n": 40, "thin": False}
OCT10 = datetime(2026, 10, 10, 15)
CUT = datetime(2026, 10, 20)  # OCT10 rows are matured
Q = "?period=custom&date=2026-10-07&to=2026-11-18"


def _r(iv=False, at=OCT10, **kw):
    return {"appliedAt": at, "interviewId": ObjectId() if iv else None, **kw}


def _g(groups, key):
    return next(g for g in groups if g["key"] == key)


def test_totals_math():
    rows = [_r(iv=True), _r(), _r(), _r(iv=True, at=datetime(2026, 11, 1)), _r(at=datetime(2026, 11, 1)),
            _r(iv=True, bidStatus="rejected")]
    t = conversion.report(rows, CUT, {}, False)["totals"]
    assert t == {"bids": 5, "matured": 3, "interviews": 1, "interviewsAll": 2, "inPlay": 2, "rate": 33.3}
    assert conversion.report([], CUT, {}, False)["totals"]["rate"] is None


def test_few_at_29_and_30():
    for n, few in ((29, True), (30, False)):
        g = conversion.report([_r(board="x.io") for _ in range(n)], CUT, {}, False)["bySite"][0]
        assert g["matured"] == n and g["few"] is few


def test_score_bands_and_other_groups():
    rows = [_r(iv=True, scoreBand="strong", area="Backend", profileAgeDays=3, board="lever.co"),
            _r(scoreBand="weak", profileAgeDays=100), _r(), _r(board="zz.io")]
    out = conversion.report(rows, CUT, {}, False)
    assert [g["label"] for g in out["byScore"]] == ["80+", "65–79", "50–64", "Under 50", "No tailored resume"]
    assert _g(out["byScore"], "strong")["rate"] == 100.0 and _g(out["byScore"], None)["bids"] == 2
    assert [g["key"] for g in out["byArea"]] == ["Backend", None]
    assert _g(out["byAge"], "<2w")["bids"] == 1 and _g(out["byAge"], "3m+")["bids"] == 1
    assert [g["key"] for g in out["bySite"]][:2] == ["lever.co", "zz.io"]
    assert _g(out["bySite"], "none")["bids"] == 2 and "bySite" in out and "byBidder" not in out


def test_bidder_table_self_first_then_bids_desc_unnamed():
    a, b, gone = ObjectId(), ObjectId(), ObjectId()
    rows = [_r(by="self")] + [_r(by="bidder", bidderId=a)] + [_r(by="bidder", bidderId=b, iv=True)] * 2 + [
        _r(by="bidder", bidderId=gone), _r(by="bidder")]
    out = conversion.report(rows, CUT, {a: "Ann", b: "Bob"}, True)["byBidder"]
    assert [(g["key"], g["label"], g["bids"]) for g in out] == [
        ("self", "Self", 1), (str(b), "Bob", 2), ("unnamed", "Unnamed bidder", 2), (str(a), "Ann", 1)]


# ---- endpoint ----
@pytest.fixture
async def db(monkeypatch):
    profile_loader.clear_cache()
    await init_beanie(database=AsyncMongoMockClient()["stats-conversion-unit"], document_models=[
        UserExtra, Account, DailyPlan, Interview, InterviewQuestion, Bid, Bidder, WeeklyPlan, Application])

    async def regions():
        return {}, {}

    async def window(today):
        return WIN

    monkeypatch.setattr(interview_rates, "_profile_regions", regions)
    monkeypatch.setattr(stats, "response_window", window)
    yield
    profile_loader.clear_cache()


async def _acct(owner, name, region="EU"):
    return await Account(name=name, region=region, createdBy=owner.id, template="x").insert()


async def _rows(acct, owner, n, iv=0, at=OCT10, **kw):
    await Application.get_motor_collection().insert_many([{
        "key": f"{acct.id}:{ObjectId()}", "accountId": acct.id, "ownerId": owner.id, "region": acct.region,
        "appliedAt": at, "interviewId": ObjectId() if i < iv else None, "by": "self", "syncedAt": at, **kw}
        for i in range(n)])


async def _get(user, qs=Q):
    app = FastAPI()
    app.include_router(stats.router)
    app.dependency_overrides[get_current_user] = lambda: user
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        return await c.get("/stats/conversion" + qs)


async def test_response_shape_and_bidders(db):
    ana = await UserExtra(name="Ana", email="a@x.io").insert()
    ben = await UserExtra(name="Ben", email="b@x.io").insert()
    bob = await Bidder(createdBy=ana.id, name="Bob", rate=1, screenshotFolderUrl="u").insert()
    a = await _acct(ana, "Alpha")
    await _rows(a, ana, 4, iv=1)
    await _rows(a, ana, 2, by="bidder", bidderId=bob.id)
    await _rows(await _acct(ben, "BenEu"), ben, 5)
    out = (await _get(ana)).json()
    assert out["startClamped"] is False and out["window"] == WIN and out["scope"] == "me"
    assert out["totals"]["bids"] == 6 and out["totals"]["interviews"] == 1 and out["totals"]["rate"] == 16.7
    assert [(g["label"], g["bids"]) for g in out["byBidder"]] == [("Self", 4), ("Bob", 2)]
    print(out)


async def test_team_scope_has_no_bidder_table(db):
    ana = await UserExtra(name="Ana", email="a@x.io").insert()
    ben = await UserExtra(name="Ben", email="b@x.io").insert()
    await _rows(await _acct(ana, "Alpha"), ana, 3)
    await _rows(await _acct(ben, "BenEu"), ben, 4)
    out = (await _get(ana, Q + "&scope=team")).json()
    assert "byBidder" not in out and out["totals"]["bids"] == 7


async def test_profile_scope_narrows(db):
    ana = await UserExtra(name="Ana", email="a@x.io").insert()
    a, b = await _acct(ana, "Alpha"), await _acct(ana, "Beta")
    await _rows(a, ana, 3)
    await _rows(b, ana, 5)
    out = (await _get(ana, Q + f"&scope=profile&profile={b.id}")).json()
    assert out["totals"]["bids"] == 5 and out["profile"] == str(b.id) and "byBidder" in out


async def test_empty_before_data_start(db):
    ana = await UserExtra(name="Ana", email="a@x.io").insert()
    await _rows(await _acct(ana, "Alpha"), ana, 3, at=datetime(2026, 9, 20, 15))
    out = (await _get(ana, "?period=custom&date=2026-09-01&to=2026-09-30")).json()
    assert out["startClamped"] is True and out["totals"]["bids"] == 0 and out["totals"]["rate"] is None


async def test_removed_endpoints_are_gone(db):
    ana = await UserExtra(name="Ana", email="a@x.io").insert()
    app = FastAPI()
    app.include_router(stats.router)
    app.dependency_overrides[get_current_user] = lambda: ana
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        for p in ("jobs", "leaders"):  # fall through to /{tab}: 422, never a report
            assert (await c.get(f"/stats/applications/{p}" + Q)).status_code in (404, 422)


def test_site_top8_other_none_and_labels():
    rows = []
    for i in range(10):
        rows += [_r(board=f"b{i}.com") for _ in range(20 - i)]
    rows += [_r(), _r(board="ashbyhq.com")]
    out = conversion.report(rows, CUT, {}, False)["bySite"]
    assert [g["key"] for g in out][:2] == ["b0.com", "b1.com"] and len(out) == 10
    assert [g["label"] for g in out][-2:] == ["Other sites", "No job link"]
    assert _g(out, "other")["bids"] == 12 + 11 + 1 and _g(out, "none")["bids"] == 1  # b8, b9, ashby
    assert conversion.report([_r(board="ashbyhq.com")], CUT, {}, False)["bySite"][0]["label"] == "Ashby"


def test_age_bucket_edges():
    rows = [_r(profileAgeDays=d) for d in (13, 14, 41, 42, 90, 91, None)]
    assert [g["bids"] for g in conversion.report(rows, CUT, {}, False)["byAge"]] == [1, 2, 2, 1, 1]
