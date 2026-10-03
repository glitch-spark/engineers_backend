"""Cells for the Google Sheet export's check columns and the per-run "Checks" tab (app/job_apply/sheet_checks.py)."""
from datetime import date
from types import SimpleNamespace as NS

from app.job_apply import sheet_checks as sc
from app.job_apply.schema import Clearance, GateResult, JobExtraction, Location
from app.models.job_apply import ProfileGates

URL = "https://jobs.example.com/1"
PASSING = [
    GateResult(name="remote", result="pass", reason="remote"),
    GateResult(name="clearance", result="pass"),
    GateResult(name="freshness", result="pass", reason="posted 2 days ago"),
]


def row(**kw):
    base = dict(
        rowIndex=4, url=URL, title="Backend Engineer", company="Acme", status="extracted", statusReason=None,
        screen="valid", groupKey="EU+US", forceInclude=False, jdStructured=True,
        fetchedPostedDate=date(2026, 10, 1), postedDate=date(2026, 10, 1),
        extraction=JobExtraction(workMode="remote", allowedLocations=[
            Location(kind="country", value="United States"), Location(kind="region", value="EU")]),
        gates=PASSING, profileGates=[], topScore=None,
    )
    base.update(kw)
    return NS(**base)


def cells(r, names=None):
    return dict(zip(sc.CHECKS_HEADER, sc.checks_row(r, names or {})))


def test_header_and_a_worthwhile_job():
    assert sc.CHECKS_HEADER == [
        "Sheet row", "Company", "Title", "Job URL", "Result", "Reason", "Posted", "Posted from", "Location",
        "Market", "Work mode", "Clearance", "Checks", "Included anyway", "Top score",
    ]
    assert sc.checks_row(row(), {}) == [
        4, "Acme", "Backend Engineer", URL, "Worth applying", "", "2026-10-01", "job page data", "United States, EU",
        "US + UK / EU", "Remote", "None", "✓ posted 2 days ago (job page data) · ✓ remote · ✓ no clearance", "", "",
    ]


def test_closed_job_says_why_and_leaves_the_rest_empty():
    c = cells(row(status="fetch_failed", statusReason="job closed (page says: This position is no longer active)",
                  screen="closed", extraction=None, gates=[], groupKey=None, fetchedPostedDate=None, postedDate=None))
    assert c["Result"] == "Closed" and c["Reason"] == "job closed (page says: This position is no longer active)"
    assert [c[k] for k in ("Posted", "Posted from", "Location", "Market", "Work mode", "Clearance", "Checks")] == [""] * 7


def test_clearance_required():
    ex = JobExtraction(workMode="remote", clearance=Clearance(level="required", note="TS/SCI"))
    gates = [PASSING[0], GateResult(name="clearance", result="fail", reason="security clearance required (TS/SCI)"), PASSING[2]]
    c = cells(row(screen="clearance", status="excluded", statusReason="clearance: security clearance required (TS/SCI)",
                  extraction=ex, gates=gates))
    assert c["Result"] == "Clearance required" and c["Clearance"] == "Required: TS/SCI"
    assert "✗ security clearance required (TS/SCI)" in c["Checks"]
    assert c["Reason"] == "clearance: security clearance required (TS/SCI)"


def test_clearance_preferred():
    ex = JobExtraction(workMode="remote", clearance=Clearance(level="preferred"))
    assert cells(row(extraction=ex))["Clearance"] == "Preferred"


def test_date_read_from_page_text_and_unknown_date():
    c = cells(row(fetchedPostedDate=None))
    assert c["Posted from"] == "read from page text" and "(read from page text)" in c["Checks"]
    gates = [PASSING[0], PASSING[1], GateResult(name="freshness", result="unknown", reason="date unknown")]
    c = cells(row(fetchedPostedDate=None, postedDate=None, gates=gates, screen="check"))
    assert c["Posted"] == "" and c["Posted from"] == "unknown"
    assert c["Checks"].startswith("? date unknown ·") and c["Reason"] == "date unknown"


def test_needs_a_check_lists_every_reason():
    ex = JobExtraction(workMode="unknown")
    gates = [GateResult(name="remote", result="unknown", reason="work mode not stated"), PASSING[1], PASSING[2]]
    c = cells(row(screen="check", groupKey="none", jdStructured=False, extraction=ex, gates=gates))
    assert c["Reason"] == "work mode not stated; location not stated; read from page text only"
    assert c["Market"] == "Not stated" and c["Location"] == "Not stated" and c["Work mode"] == "Not stated"
    assert "? remote: work mode not stated" in c["Checks"] and c["Checks"].endswith("? read from page text only")


def test_included_anyway():
    gates = [PASSING[0], PASSING[1], GateResult(name="freshness", result="fail", reason="posted 57 days ago")]
    c = cells(row(screen="check", forceInclude=True, gates=gates))
    assert c["Included anyway"] == "Yes" and "✗ posted 57 days ago (job page data)" in c["Checks"]
    assert "included anyway" in c["Reason"]


def test_on_site_and_other_locations():
    ex = JobExtraction(workMode="onsite", allowedLocations=[Location(kind="country", value="India")])
    gates = [GateResult(name="remote", result="fail", reason="onsite"), PASSING[1], PASSING[2]]
    c = cells(row(screen="onsite", extraction=ex, gates=gates))
    assert c["Work mode"] == "On-site" and "✗ remote: onsite" in c["Checks"]
    c = cells(row(screen="other_location", groupKey="IN", extraction=ex.model_copy(update={"workMode": "remote"})))
    assert c["Result"] == "Other locations" and c["Market"] == "Other (IN)" and c["Reason"] == "open to candidates in India"


def test_not_a_job_page_and_not_checked_yet():
    c = cells(row(screen="not_job", extraction=JobExtraction(isJobPosting=False)))
    assert c["Result"] == "Not a job page" and "✗ not a job page" in c["Checks"]
    assert cells(row(status="pending", screen=None, extraction=None, gates=[]))["Result"] == "Not checked yet"


def test_scored_job_shows_each_profiles_checks_and_top_score():
    pg = [ProfileGates(accountId="a1", gates=[GateResult(name="location", result="pass", reason="United States, EU"),
                                              GateResult(name="workAuth", result="pass")]),
          ProfileGates(accountId="a2", gates=[GateResult(name="location", result="fail", reason="allows United States, EU; profile is BR"),
                                              GateResult(name="workAuth", result="pass")])]
    c = cells(row(profileGates=pg, topScore=82), {"a1": "Alex", "a2": "Bia"})
    assert c["Top score"] == 82
    assert c["Checks"].endswith(" | Alex: ✓ location: United States, EU · ✓ work auth"
                                " | Bia: ✗ location: allows United States, EU; profile is BR · ✓ work auth")


def test_apply_columns_are_one_profiles_view():
    pg = [ProfileGates(accountId="a1", gates=[GateResult(name="location", result="unknown", reason="location not stated"),
                                              GateResult(name="workAuth", result="pass")])]
    assert sc.APPLY_HEADER == ["Location", "Work mode", "Clearance", "Checks"]
    assert sc.apply_cells(row(profileGates=pg), "a1") == [
        "United States, EU", "Remote", "None",
        "✓ posted 2 days ago (job page data) · ✓ remote · ✓ no clearance · ? location: location not stated · ✓ work auth",
    ]


def test_text_that_looks_like_a_formula_is_escaped():
    c = cells(row(title="=HYPERLINK(\"https://evil.example\")", company="-Acme"))
    assert c["Title"] == "'=HYPERLINK(\"https://evil.example\")" and c["Company"] == "'-Acme"
    assert sc.sheet_text("") == "" and sc.sheet_text(5) == 5 and sc.sheet_text("@x") == "'@x"


def test_tab_name_is_per_run_and_sheet_safe():
    run = NS(fileName="Jobs [Oct]: list/1.xlsx", id="6ac0b0831f138761131726de")
    assert sc.checks_tab_name(run) == "Checks · Jobs Oct list1.xlsx · 1726de"
    assert len(sc.checks_tab_name(NS(fileName="x" * 300, id="6ac0b0831f138761131726de"))) <= 100
