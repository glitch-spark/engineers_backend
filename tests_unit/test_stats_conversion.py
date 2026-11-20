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
        assert g["fewReason"] == ("matured" if few else None)


def test_few_needs_both_few_interviews_and_few_expected():
    """Changed 2026-11-19 after real data: 20 interviews from 339 matured (team 0.6) is not few."""
    def site(n, iv, rate):
        rows = [_r(board="x.io", iv=i < iv) for i in range(n)]
        g = conversion.report(rows, CUT, {}, False, rate)["bySite"][0]
        return g["few"], g["fewReason"]
    assert site(339, 20, 0.6) == (False, None)
    assert site(271, 1, 0.6) == (True, "expected")   # 1 interview, 1.6 expected
    assert site(29, 5, 0.6) == (True, "matured")


def test_few_by_expected_interviews_at_team_rate():
    """I2: few also when matured x team rate / 100 < 3 (expected interviews), the grades' EXPECTED_MIN rule."""
    def site(n, rate):
        return conversion.report([_r(board="x.io") for _ in range(n)], CUT, {}, False, rate)["bySite"][0]
    assert (site(600, 0.5)["few"], site(600, 0.5)["fewReason"]) == (False, None)        # 3.0 expected
    assert (site(300, 0.5)["few"], site(300, 0.5)["fewReason"]) == (True, "expected")   # 1.5 expected
    assert (site(29, 0.5)["few"], site(29, 0.5)["fewReason"]) == (True, "matured")
    assert (site(600, 0.6)["few"], site(600, 0.6)["fewReason"]) == (False, None)        # 3.6 expected, 0 interviews
    assert (site(300, None)["few"], site(300, 0.0)["few"]) == (False, False)            # no team rate: matured only
    assert conversion.report([], CUT, {}, False, 0.54)["teamRate"] == 0.5
    assert conversion.report([], CUT, {}, False)["teamRate"] is None


def test_score_bands_and_other_groups():
    rows = [_r(iv=True, scoreBand="strong", area="Backend", profileAgeDays=3, board="lever.co"),
            _r(scoreBand="weak", profileAgeDays=100), _r(), _r(board="zz.io")]
    out = conversion.report(rows, CUT, {}, False)
    assert [g["label"] for g in out["byScore"]] == ["80+", "65–79", "50–64", "Under 50", "No score"]
    assert _g(out["byScore"], "strong")["rate"] == 100.0 and _g(out["byScore"], None)["bids"] == 2
    assert [g["key"] for g in out["byArea"]] == ["Backend", None]
    assert _g(out["byAge"], "0-2w")["bids"] == 1 and _g(out["byAge"], "12w+")["bids"] == 1
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
    # team rate = the team rows' pooled matured rate (Ana 1 of 6, Ben 0 of 5); 11 matured < 30: few by "matured"
    assert out["teamRate"] == 9.1 and out["byBidder"][0]["fewReason"] == "matured"


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
    rows = [_r(profileAgeDays=d) for d in (13, 14, 27, 28, 41, 42, 83, 84, None)]
    assert [g["bids"] for g in conversion.report(rows, CUT, {}, False)["byAge"]] == [1, 2, 2, 2, 1, 1]


async def test_interview_only_rows_count_in_conversion(db):
    ana = await UserExtra(name="Ana", email="a@x.io").insert()
    a = await _acct(ana, "Alpha")
    await _rows(a, ana, 3, iv=1)
    await _rows(a, ana, 1, iv=1, byInterviewOnly=True)
    t = (await _get(ana)).json()["totals"]
    assert t["bids"] == 4 and t["interviews"] == 2 and t["interviewsAll"] == 2


async def _iv(acct, owner, first, rounds=None, **kw):
    ivs = Interview.get_motor_collection()
    doc = {"accountId": acct.id, "createdBy": owner.id, "scheduledAt": first, "stage": "intro", "status": "completed",
           "stageHistory": rounds or [], "createdAt": first, "updatedAt": first, **kw}
    return (await ivs.insert_one(doc)).inserted_id


async def test_untied_counts_interviews_no_row_carries(db):
    ana = await UserExtra(name="Ana", email="a@x.io").insert()
    ben = await UserExtra(name="Ben", email="b@x.io").insert()
    a, b = await _acct(ana, "Alpha"), await _acct(ben, "BenEu")
    await _rows(a, ana, 2)
    carried = await _iv(a, ana, OCT10)
    await Application.get_motor_collection().update_one({}, {"$set": {"interviewId": carried}})
    await _iv(a, ana, OCT10)                                                   # untied
    await _iv(a, ana, OCT10, status="canceled", stage="intro", stageHistory=[])  # canceled only: not countable
    await _iv(a, ana, datetime(2026, 9, 20, 15))                               # first round before the period
    await _iv(b, ben, OCT10)                                                   # other profile
    out = (await _get(ana)).json()["totals"]
    assert out["untied"] == 1                                                  # not double counted: carried is out
    assert (await _get(ana, Q + "&scope=team")).json()["totals"]["untied"] == 2
    assert (await _get(ana, Q + f"&scope=profile&profile={b.id}")).status_code == 403


async def test_untied_uses_first_round_in_period_and_archived_profile(db):
    ana = await UserExtra(name="Ana", email="a@x.io").insert()
    gone = await Account(name="Gone", region="EU", archived=True, createdBy=ana.id, template="x").insert()
    a = await _acct(ana, "Alpha")
    await _rows(a, ana, 1)
    # created before the period but its first round (scheduledAt) is inside it: counts
    await _iv(gone, ana, OCT10, createdAt=datetime(2026, 9, 1))
    # created inside the period but its first round is before it: out
    await _iv(a, ana, datetime(2026, 9, 20, 15), createdAt=OCT10)
    assert (await _get(ana)).json()["totals"]["untied"] == 1


async def test_untied_prefilter_skips_far_interviews_and_keeps_edges(db, monkeypatch):
    ana = await UserExtra(name="Ana", email="a@x.io").insert()
    a = await _acct(ana, "Alpha")
    far = await _iv(a, ana, datetime(2026, 8, 1))                       # all rounds far outside
    await _iv(a, ana, datetime(2026, 8, 1), rounds=[{"stage": "intro", "status": "completed", "at": OCT10, "scheduledAt": OCT10}])
    seen = []
    orig = profile_loader.Interview.model_validate
    monkeypatch.setattr(profile_loader.Interview, "model_validate", lambda d, *x, **k: (seen.append(d["_id"]), orig(d, *x, **k))[1])
    assert (await _get(ana)).json()["totals"]["untied"] == 1
    assert far not in seen


def test_self_vs_bidder_in_every_scope():
    """Self vs bidder bids (user, 2026-11-19): two groups, no bidder names, shown for team too."""
    rows = [_r(iv=True, by="self"), _r(by="self"), _r(by="bidder", bidderId=ObjectId()), _r(by="bidder"),
            _r(by="bidder", iv=True), _r(by="bidder")]
    for include_bidders in (True, False):
        out = conversion.report(rows, CUT, {}, include_bidders)
        assert [(g["key"], g["label"], g["bids"], g["interviews"]) for g in out["byWho"]] == [
            ("self", "Self", 2, 1), ("bidder", "Bidder", 4, 1)]
