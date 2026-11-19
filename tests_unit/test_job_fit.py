"""job_fit.breakdown: groupings, labels, too-few, rejected, known share; loader cache (pure / mongomock)."""
import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from datetime import datetime, timedelta

import pytest
from beanie import init_beanie
from bson import ObjectId
from mongomock_motor import AsyncMongoMockClient

from app.models import Application
from app.stats import job_fit, profile_loader

NOW = datetime(2026, 11, 18)
CUT = NOW - timedelta(days=10)
OLD = NOW - timedelta(days=60)


def row(n=1, iv=0, at=OLD, status=None, **kw):
    return [{"appliedAt": at, "bidStatus": status, "interviewId": ObjectId() if i < iv else None, **kw}
            for i in range(n)]


def g(out, section, key):
    return next(x for x in out[section] if x["key"] == key)


def test_site_groups_top8_other_and_no_link():
    team = []
    for i in range(10):
        team += row(20 - i, board=f"b{i}.com")
    team += row(2, board=None)
    out = job_fit.breakdown(row(3, board="b0.com") + row(1, board="b9.com") + row(1, board=None), team, CUT)
    keys = [x["key"] for x in out["bySite"]]
    assert keys == [f"b{i}.com" for i in range(8)] + ["other", "none"]
    assert [x["label"] for x in out["bySite"]][-2:] == ["Other sites", "No job link"]
    assert g(out, "bySite", "other")["team"]["bids"] == 12 + 11  # b8, b9
    assert g(out, "bySite", "b0.com")["you"]["bids"] == 3
    assert g(out, "bySite", "none")["team"]["bids"] == 2


def test_site_labels():
    boards = ["ashbyhq.com", "greenhouse.io", "myworkdayjobs.com", "workable.com", "lever.co", "bamboohr.com",
              "rippling.com", "applytojob.com"]
    out = job_fit.breakdown([], [r for i, b in enumerate(boards) for r in row(10 - i, board=b)], CUT)
    assert [x["label"] for x in out["bySite"][:8]] == ["Ashby", "Greenhouse", "Workday", "Workable", "Lever",
                                                       "BambooHR", "Rippling", "JazzHR"]
    out = job_fit.breakdown([], row(2, board="foo.example"), CUT)
    assert out["bySite"][0]["label"] == "foo.example"
    for d, lab in [("smartrecruiters.com", "SmartRecruiters"), ("breezy.hr", "Breezy"), ("oraclecloud.com", "Oracle"),
                   ("zohorecruit.com", "Zoho Recruit"), ("icims.com", "iCIMS"), ("teamtailor.com", "Teamtailor"),
                   ("linkedin.com", "LinkedIn")]:
        assert job_fit.breakdown([], row(1, board=d), CUT)["bySite"][0]["label"] == lab


def test_score_bands_and_no_resume():
    team = [r for b in ("strong", "good", "fair", "weak", None) for r in row(1, scoreBand=b)]
    out = job_fit.breakdown([], team, CUT)
    assert [(x["key"], x["label"]) for x in out["byScore"]] == [
        ("strong", "80+"), ("good", "65–79"), ("fair", "50–64"), ("weak", "Under 50"), (None, "No tailored resume")]


def test_area_unknown_and_empty_groups_omitted():
    team = row(1, area="Backend") + row(1, area="AI / ML") + row(1, area=None)
    out = job_fit.breakdown(row(1, area="Data"), team, CUT)
    assert [x["label"] for x in out["byArea"]] == ["Backend", "AI / ML", "Data", "Unknown"]


def test_age_buckets_edges():
    team = [r for d in (13, 14, 41, 42, 90, 91, None) for r in row(1, profileAgeDays=d)]
    out = job_fit.breakdown([], team, CUT)
    assert [x["label"] for x in out["byAge"]] == ["Under 2 weeks", "2–6 weeks", "6 weeks – 3 months", "3+ months", "Unknown"]
    assert [x["team"]["bids"] for x in out["byAge"]] == [1, 2, 2, 1, 1]  # 91 -> 3+, 90 -> 6w-3m, None -> Unknown


def test_too_few_under_30_matured():
    out = job_fit.breakdown([], row(29, iv=2, area="Data") + row(30, iv=3, area="Backend"), CUT)
    d, b = g(out, "byArea", "Data")["team"], g(out, "byArea", "Backend")["team"]
    assert d["tooFew"] is True and d["per100"] == 6.9 and d["interviews"] == 2
    assert b["tooFew"] is False and b["per100"] == 10.0


def test_matured_cutoff_and_per100_none():
    out = job_fit.breakdown([], row(5, at=NOW, area="Data"), CUT)
    t = g(out, "byArea", "Data")["team"]
    assert t["bids"] == 5 and t["matured"] == 0 and t["per100"] is None and t["tooFew"] is True


def test_rejected_excluded():
    team = row(3, area="Data") + row(4, status="rejected", area="Data")
    out = job_fit.breakdown(row(1, status="rejected", area="Data"), team, CUT)
    assert g(out, "byArea", "Data")["team"]["bids"] == 3
    assert g(out, "byArea", "Data")["you"]["bids"] == 0
    assert out["known"] == {"score": None, "area": None}


def test_known_percentages_sparse():
    you = row(5, scoreBand="good", area="Data") + row(95)
    assert job_fit.breakdown(you, you, CUT)["known"] == {"score": 5, "area": 5}


async def test_loader_cache_reused_and_clearable():
    await init_beanie(database=AsyncMongoMockClient()["job-fit-unit"], document_models=[Application])
    profile_loader.clear_cache()
    a = (start := NOW - timedelta(days=90)), NOW
    await Application(key="k1", ownerId=ObjectId(), appliedAt=OLD, syncedAt=NOW, board="lever.co", scoreBand="good",
                      score=70, area="Data", profileAgeDays=5, templateName="T", bidderId=ObjectId()).insert()
    rows = await profile_loader.load_period_rows(*a, "all")
    assert len(rows) == 1 and rows[0]["board"] == "lever.co" and rows[0]["scoreBand"] == "good"
    for f in ("board", "scoreBand", "score", "area", "profileAgeDays", "bidderId", "templateName"):
        assert f in rows[0]
    await Application(key="k2", ownerId=ObjectId(), appliedAt=OLD, syncedAt=NOW).insert()
    assert len(await profile_loader.load_period_rows(*a, "all")) == 1  # cached
    assert len(await profile_loader.load_period_rows(*a, "EU")) == 0   # other key
    profile_loader.clear_cache()
    assert len(await profile_loader.load_period_rows(*a, "all")) == 2
    profile_loader.clear_cache()


async def test_loader_cache_expires_after_ttl(monkeypatch):
    await init_beanie(database=AsyncMongoMockClient()["job-fit-ttl"], document_models=[Application])
    profile_loader.clear_cache()
    clock = [1000.0]
    monkeypatch.setattr(profile_loader.time, "monotonic", lambda: clock[0])
    a = NOW - timedelta(days=90), NOW
    await Application(key="k1", ownerId=ObjectId(), appliedAt=OLD, syncedAt=NOW).insert()
    await profile_loader.load_period_rows(*a, "all")
    await Application(key="k2", ownerId=ObjectId(), appliedAt=OLD, syncedAt=NOW).insert()
    clock[0] += profile_loader._TTL - 1
    assert len(await profile_loader.load_period_rows(*a, "all")) == 1
    clock[0] += 2
    assert len(await profile_loader.load_period_rows(*a, "all")) == 2
    await profile_loader.load_period_rows(*a, "EU")
    clock[0] += profile_loader._TTL + 1
    await profile_loader.load_period_rows(*a, "US")  # a miss drops the expired entries
    assert list(profile_loader._cache) == [(*a, "US")]
    profile_loader.clear_cache()


async def test_loader_cache_capped(monkeypatch):
    await init_beanie(database=AsyncMongoMockClient()["job-fit-cap"], document_models=[Application])
    profile_loader.clear_cache()
    clock = [0.0]
    monkeypatch.setattr(profile_loader.time, "monotonic", lambda: clock[0])
    for i in range(20):
        clock[0] += 1
        await profile_loader.load_period_rows(NOW - timedelta(days=i + 1), NOW, "all")
    assert len(profile_loader._cache) == 16
    assert (NOW - timedelta(days=1), NOW, "all") not in profile_loader._cache  # oldest evicted
    assert (NOW - timedelta(days=20), NOW, "all") in profile_loader._cache
    profile_loader.clear_cache()
