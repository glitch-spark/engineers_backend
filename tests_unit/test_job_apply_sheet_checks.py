"""Cells for the Google Sheet export's job columns and the per-run "Checks" tab (app/job_apply/sheet_checks.py)."""
from datetime import date
from types import SimpleNamespace as NS

from app.job_apply import sheet_checks as sc
from app.job_apply.schema import Clearance, JobExtraction, Location

URL = "https://jobs.example.com/1"


def row(**kw):
    base = dict(
        url=URL, title="Backend Engineer", company="Acme", screen="valid", postedDate=date(2026, 10, 1),
        extraction=JobExtraction(workMode="remote", allowedLocations=[
            Location(kind="country", value="United States"), Location(kind="region", value="EU")]),
    )
    base.update(kw)
    return NS(**base)


def cells(r):
    return dict(zip(sc.CHECKS_HEADER, sc.checks_row(r)))


def test_header_and_a_worthwhile_job():
    assert sc.CHECKS_HEADER == [
        "Company Name", "Job URL", "Job Title", "Posting Date", "Location", "Security Clearance",
        "Remote/Hybrid/Onsite", "Status",
    ]
    assert sc.checks_row(row()) == [
        "Acme", URL, "Backend Engineer", "2026-10-01", "United States, EU", "None", "Remote", "Worth applying",
    ]


def test_closed_job_has_only_url_and_status():
    c = cells(row(screen="closed", extraction=None, postedDate=None, company="", title=""))
    assert c["Status"] == "Closed" and c["Job URL"] == URL
    assert [c[k] for k in ("Company Name", "Job Title", "Posting Date", "Location", "Security Clearance",
                           "Remote/Hybrid/Onsite")] == [""] * 6


def test_company_and_title_fall_back_to_what_was_read():
    c = cells(row(company="", title="", extraction=JobExtraction(company="Beta", title="Data Engineer")))
    assert c["Company Name"] == "Beta" and c["Job Title"] == "Data Engineer"


def test_security_clearance():
    req = JobExtraction(clearance=Clearance(level="required", note="TS/SCI"))
    assert cells(row(screen="clearance", extraction=req))["Security Clearance"] == "Required: TS/SCI"
    assert cells(row(extraction=JobExtraction(clearance=Clearance(level="preferred"))))["Security Clearance"] == "Preferred"
    assert cells(row(screen="clearance", extraction=req))["Status"] == "Clearance required"


def test_work_mode_and_unstated_values():
    assert cells(row(extraction=JobExtraction(workMode="hybrid")))["Remote/Hybrid/Onsite"] == "Hybrid"
    assert cells(row(extraction=JobExtraction(workMode="onsite")))["Remote/Hybrid/Onsite"] == "On-site"
    c = cells(row(screen="check", postedDate=None, extraction=JobExtraction()))
    assert c["Remote/Hybrid/Onsite"] == "Not stated" and c["Location"] == "Not stated" and c["Posting Date"] == ""
    assert c["Status"] == "Needs a check"


def test_every_status_label():
    labels = {b: cells(row(screen=b))["Status"] for b in sc.STATUS_LABEL}
    assert labels == {
        "valid": "Worth applying", "check": "Needs a check", "closed": "Closed", "not_fetched": "Not fetched",
        "read_failed": "Couldn't read", "not_job": "Not a job page", "clearance": "Clearance required",
        "onsite": "On-site / hybrid", "too_old": "Too old", "other_location": "Other locations",
    }
    assert cells(row(screen=None, extraction=None))["Status"] == "Not checked yet"


def test_apply_columns():
    assert sc.APPLY_HEADER == ["Location", "Security Clearance", "Remote/Hybrid/Onsite"]
    assert sc.apply_cells(row()) == ["United States, EU", "None", "Remote"]


def test_text_that_looks_like_a_formula_is_escaped():
    c = cells(row(title="=HYPERLINK(\"https://evil.example\")", company="-Acme"))
    assert c["Job Title"] == "'=HYPERLINK(\"https://evil.example\")" and c["Company Name"] == "'-Acme"
    assert sc.sheet_text("") == "" and sc.sheet_text(5) == 5 and sc.sheet_text("@x") == "'@x"


def test_tab_name_is_per_run_and_sheet_safe():
    run = NS(fileName="Jobs [Oct]: list/1.xlsx", id="6ac0b0831f138761131726de")
    assert sc.checks_tab_name(run) == "Checks · Jobs Oct list1.xlsx · 1726de"
    assert len(sc.checks_tab_name(NS(fileName="x" * 300, id="6ac0b0831f138761131726de"))) <= 100
