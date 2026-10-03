"""Unit tests for screening: one bucket per job, location groups and which profiles fit them (no network, no DB)."""
import os
from datetime import date, timedelta

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from app.job_apply.schema import GateResult, JobExtraction, Location  # noqa: E402
from app.job_apply.screening import bucket, build_report, group_key, profile_fits, rescreen  # noqa: E402

RUN = date(2026, 10, 2)


def gates(remote="pass", clearance="pass", freshness="pass"):
    return [
        GateResult(name="remote", result=remote, reason="hybrid" if remote == "fail" else ""),
        GateResult(name="clearance", result=clearance),
        GateResult(name="freshness", result=freshness, reason="posted 40 days ago" if freshness == "fail" else ""),
    ]


def b(status="extracted", reason=None, g=None, key="GB", force=False):
    return bucket(status=status, status_reason=reason, gates=g if g is not None else gates(), group_key=key, force_include=force)


def test_bucket_order():
    assert b("fetch_failed", "job closed (HTTP 410)", []) == "closed"
    assert b("fetch_failed", "no job text on the page", []) == "not_fetched"
    assert b("llm_failed", "boom", []) == "read_failed"
    assert b("excluded", g=gates(remote="fail", clearance="fail")) == "clearance"
    assert b("excluded", g=gates(remote="fail", freshness="fail")) == "onsite"
    assert b("excluded", g=gates(freshness="fail")) == "too_old"
    assert b(g=gates(freshness="unknown")) == "check"
    assert b(key="none") == "check"
    assert b() == "valid"
    assert b("scored") == "valid"
    assert b("pending", g=[]) is None and b("fetched", g=[]) is None


def test_force_include():
    assert b("excluded", g=gates(freshness="fail"), force=True) == "check"
    assert b("excluded", g=gates(remote="fail", clearance="fail"), force=True) == "check"
    assert b("fetch_failed", "no job text on the page", [], force=True) == "not_fetched"


def test_group_key():
    c = lambda v: Location(kind="country", value=v)  # noqa: E731
    r = lambda v: Location(kind="region", value=v)  # noqa: E731
    assert group_key([c("United Kingdom"), c("GB")]) == "GB"
    assert group_key([c("US"), r("EU")]) == "EU+US"
    assert group_key([r("worldwide")]) == "Worldwide"
    assert group_key([r("Europe")]) == "Europe"
    assert group_key([r("emea")]) == "EMEA"
    assert group_key([r("United States")]) == "US"
    assert group_key([]) == "none"


def test_profile_fits():
    assert profile_fits("GB", country="GB", region="EU")
    assert not profile_fits("GB", country="US", region="US")
    assert profile_fits("Europe", country="DE", region=None)
    assert profile_fits("EU+US", country="US", region=None)
    assert profile_fits("none", country="US", region=None)
    assert profile_fits("US", country=None, region=None)
    assert profile_fits("Worldwide", country="BR", region=None)
    # Locations the gates can't read count as unknown (a pass) at scoring time, so the report agrees.
    assert profile_fits("Mars Colony", country="US", region=None)
    assert profile_fits("Remote", country="US", region="US")
    assert not profile_fits("GB+Remote", country="US", region="US")


class _Row:
    """The fields rescreen reads and writes (a JobApplyRow without Beanie)."""

    def __init__(self, posted, status="excluded"):
        self.extraction = JobExtraction(title="T", workMode="remote", allowedLocations=[Location(kind="country", value="GB")])
        self.postedDate = posted
        self.status, self.statusReason = status, None
        self.gates, self.screen, self.groupKey, self.forceInclude = [], None, "GB", False


def test_rescreen_max_age():
    row = _Row(RUN - timedelta(days=40))
    rescreen(row, run_date=RUN, max_age_days=60)
    assert row.status == "extracted" and row.screen == "valid" and row.statusReason is None
    rescreen(row, run_date=RUN, max_age_days=30)
    assert row.status == "excluded" and row.screen == "too_old" and "posted 40 days ago" in row.statusReason


def test_rescreen_leaves_scored_and_failed_status():
    row = _Row(RUN - timedelta(days=5), status="fetch_failed")
    row.extraction = None
    rescreen(row, run_date=RUN, max_age_days=30)
    assert row.status == "fetch_failed"


def test_build_report():
    sam = {"id": "s", "name": "Sam", "country": "GB", "region": "EU", "resumes": 2}
    alex = {"id": "a", "name": "Alex", "country": "US", "region": "US", "resumes": 1}
    rep = build_report(
        [("valid", "GB"), ("check", "none"), ("closed", None), ("too_old", "US")], [alex, sam], max_age_days=30
    )
    assert rep["total"] == 4 and rep["worth"] == 2 and rep["check"] == 1 and rep["maxAgeDays"] == 30
    assert rep["buckets"]["closed"] == 1 and rep["buckets"]["not_fetched"] == 0 and len(rep["buckets"]) == 8
    assert rep["groups"] == [
        {"key": "GB", "jobs": 1, "check": 0, "fits": ["s"]},
        {"key": "none", "jobs": 1, "check": 1, "fits": ["a", "s"]},
    ]
    assert rep["profiles"] == [alex, sam]


def test_build_report_groups_sorted_by_size():
    rep = build_report([("valid", "US"), ("valid", "GB"), ("valid", "GB")], [], max_age_days=30)
    assert [g["key"] for g in rep["groups"]] == ["GB", "US"]
