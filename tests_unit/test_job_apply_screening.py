"""Unit tests for screening: one bucket per job, location groups and which profiles fit them (no network, no DB)."""
import os
from datetime import date, timedelta

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from app.job_apply.schema import GateResult, JobExtraction, Location  # noqa: E402
from app.job_apply.screening import (  # noqa: E402
    APPROVABLE,
    WORTH,
    bucket,
    build_report,
    group_key,
    markets_for,
    profile_market,
    rescreen,
    scoring_key,
)

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


def test_not_a_job_page():
    assert bucket(status="extracted", status_reason=None, gates=gates(), group_key="none", force_include=False,
                  is_job=False) == "not_job"
    assert bucket(status="excluded", status_reason=None, gates=gates(freshness="fail"), group_key="GB",
                  force_include=True, is_job=False) == "not_job"


def test_unverified_page_needs_a_check():
    # read only from page text (no job-site API, no JobPosting data): kept, but flagged
    assert bucket(status="extracted", status_reason=None, gates=gates(), group_key="GB", force_include=False,
                  verified=False) == "check"
    assert bucket(status="excluded", status_reason=None, gates=gates(freshness="fail"), group_key="GB",
                  force_include=False, verified=False) == "too_old"


def test_approved_job_is_worth_applying():
    # a person approved it: every check that sent it elsewhere is overruled, and it is Worth applying (not Needs a check)
    assert b("excluded", g=gates(freshness="fail"), force=True) == "valid"
    assert b("excluded", g=gates(remote="fail", clearance="fail"), force=True) == "valid"
    assert b(g=gates(freshness="unknown"), force=True) == "valid"
    assert b(key="none", force=True) == "valid"
    assert b(key="CA", force=True) == "valid"
    assert bucket(status="extracted", status_reason=None, gates=gates(), group_key="GB", force_include=True,
                  verified=False) == "valid"


def test_approval_needs_a_description_to_work_on():
    # no readable posting yet (fetch / read failed, or not a job page): approval changes nothing until the
    # description is pasted in Job info and the job is read again
    assert b("fetch_failed", "no job text on the page", [], force=True) == "not_fetched"
    assert b("fetch_failed", "job closed (HTTP 410)", [], force=True) == "closed"
    assert b("llm_failed", "boom", [], force=True) == "read_failed"


def test_approvable_buckets():
    assert set(APPROVABLE) == {"check", "clearance", "onsite", "too_old", "other_location"}


def test_scoring_key():
    # an approved job with no target market is scored like one that states no location; anything else keeps its group
    assert scoring_key("CA", True) == "none" and scoring_key("APAC", True) == "none"
    assert scoring_key("CA", False) == "CA"
    assert scoring_key("GB", True) == "GB" and scoring_key("CA+US", True) == "CA+US"
    assert scoring_key("none", True) == "none" and scoring_key(None, True) is None


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
    assert group_key([r("Western EU")]) == "Europe" and group_key([r("Central Europe")]) == "Europe"
    assert group_key([r("CET +/-2")]) == "EMEA" and group_key([r("GMT±3")]) == "EMEA"
    assert group_key([r("EST hours")]) == "North America"


class _Row:
    """The fields rescreen reads and writes (a JobApplyRow without Beanie)."""

    def __init__(self, posted, status="excluded"):
        self.extraction = JobExtraction(title="T", workMode="remote", allowedLocations=[Location(kind="country", value="GB")])
        self.postedDate = posted
        self.status, self.statusReason = status, None
        self.gates, self.screen, self.groupKey, self.forceInclude = [], None, "GB", False
        self.jdStructured = None


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


def test_markets_for():
    assert markets_for("US") == ["US"]
    assert markets_for("GB") == ["UKEU"] and markets_for("DE+ES+GB") == ["UKEU"]
    assert markets_for("EMEA") == ["UKEU"] and markets_for("Europe") == ["UKEU"] and markets_for("EU") == ["UKEU"]
    assert markets_for("BR") == ["LATAM"] and markets_for("Latam") == ["LATAM"]
    assert markets_for("Americas") == ["US", "LATAM"] and markets_for("North America") == ["US"]
    assert markets_for("Worldwide") == ["US", "UKEU", "LATAM"]
    assert markets_for("EU+US") == ["US", "UKEU"]
    assert markets_for("CA") == [] and markets_for("APAC") == [] and markets_for("IL") == [] and markets_for("Remote") == []
    assert markets_for("none") == []


def test_profile_market():
    assert profile_market(country="US", region="US") == "US"
    assert profile_market(country="GB", region=None) == "UKEU"
    assert profile_market(country=None, region="EU") == "UKEU"
    assert profile_market(country="BR", region="Latam") == "LATAM"
    assert profile_market(country="CA", region="Canada") is None
    assert profile_market(country=None, region=None) is None


def test_other_location_bucket():
    assert b(key="CA") == "other_location"
    assert b(key="APAC", g=gates(freshness="unknown")) == "other_location"
    assert b(key="CA", g=gates(freshness="fail")) == "too_old"
    assert b(key="CA+US") == "valid"


def test_build_report():
    sam = {"id": "s", "name": "Sam", "country": "GB", "region": "EU", "resumes": 2}
    alex = {"id": "a", "name": "Alex", "country": "US", "region": "US", "resumes": 1}
    rep = build_report(
        [("valid", "GB"), ("check", "none"), ("closed", None), ("too_old", "US"), ("valid", "Worldwide"),
         ("other_location", "CA"), ("other_location", "CA"), ("other_location", "APAC")],
        [alex, sam], max_age_days=30,
    )
    # a Needs-a-check job isn't worth applying to (or scored) until a person approves it
    assert rep["total"] == 8 and rep["worth"] == 2 and rep["check"] == 1 and rep["maxAgeDays"] == 30
    assert rep["buckets"]["closed"] == 1 and rep["buckets"]["other_location"] == 3 and len(rep["buckets"]) == 10
    # the three target markets always show, then "not stated" when it has jobs; a Worldwide job is in every market
    assert rep["groups"] == [
        {"key": "US", "jobs": 1, "fits": ["a"]},
        {"key": "UKEU", "jobs": 2, "fits": ["s"]},
        {"key": "LATAM", "jobs": 1, "fits": []},
    ]
    assert rep["others"] == [{"key": "CA", "jobs": 2}, {"key": "APAC", "jobs": 1}]
    # each worthwhile job once, by the set of markets it's in, so a profile's jobs aren't counted twice
    assert rep["combos"] == [
        {"markets": ["UKEU"], "jobs": 1},
        {"markets": ["US", "UKEU", "LATAM"], "jobs": 1},
    ]
    assert rep["profiles"] == [alex, sam]


def test_build_report_after_approval():
    alex = {"id": "a", "name": "Alex", "country": "US", "region": "US", "resumes": 1}
    before = build_report([("valid", "US"), ("check", "none"), ("other_location", "CA")], [alex], max_age_days=30)
    assert before["check"] == 1 and before["worth"] == 1 and before["others"] == [{"key": "CA", "jobs": 1}]
    assert [g["key"] for g in before["groups"]] == ["US", "UKEU", "LATAM"]
    # approving the Needs-a-check job and the Canada-only job: both become Worth applying, the Canada one is
    # counted in the "location not stated" group (scoring_key), and nothing is left needing a check
    after = build_report(
        [("valid", "US"), ("valid", scoring_key("none", True)), ("valid", scoring_key("CA", True))], [alex], max_age_days=30
    )
    assert after["check"] == 0 and after["worth"] == 3 and after["others"] == []
    assert after["buckets"]["valid"] == 3 and after["buckets"]["check"] == 0
    assert [g["key"] for g in after["groups"]] == ["US", "UKEU", "LATAM", "none"]
    assert after["groups"][-1] == {"key": "none", "jobs": 2, "fits": ["a"]}


def test_only_worth_applying_jobs_are_scored():
    # Needs a check is scored only once a person approves it (it is then Worth applying)
    assert WORTH == ("valid",)
    assert "check" in APPROVABLE


def test_build_report_profile_without_location_fits_every_market():
    nowhere = {"id": "n", "name": "N", "country": None, "region": None, "resumes": 1}
    rep = build_report([("valid", "US")], [nowhere], max_age_days=30)
    assert all(g["fits"] == ["n"] for g in rep["groups"])


def test_remote_area_limit_needs_a_check():
    area = GateResult(name="area", result="unknown", reason="remote only in Austin Metro")
    assert b(g=[*gates(), area]) == "check"
