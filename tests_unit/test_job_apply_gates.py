"""Unit tests for job-level and per-profile eligibility gates (spec §3.7)."""
from datetime import date

from app.job_apply.gates import blocked, covers, job_gates, profile_gates
from app.job_apply.schema import Clearance, JobExtraction, Location, WorkAuth

RUN = date(2026, 10, 1)


def ex(**kw):
    return JobExtraction(**kw)


def by_name(gates):
    return {g.name: g for g in gates}


def test_job_gate_names():
    assert [g.name for g in job_gates(ex(), run_date=RUN, max_age_days=30)] == ["remote", "clearance", "freshness"]


def test_remote_gate():
    assert by_name(job_gates(ex(workMode="hybrid"), run_date=RUN, max_age_days=30))["remote"].result == "fail"
    assert by_name(job_gates(ex(workMode="onsite"), run_date=RUN, max_age_days=30))["remote"].result == "fail"
    assert by_name(job_gates(ex(workMode="remote"), run_date=RUN, max_age_days=30))["remote"].result == "pass"
    assert by_name(job_gates(ex(workMode="unknown"), run_date=RUN, max_age_days=30))["remote"].result == "unknown"


def test_clearance_gate():
    g = by_name(job_gates(ex(workMode="remote", clearance=Clearance(level="required")), run_date=RUN, max_age_days=30))
    assert g["clearance"].result == "fail"
    g = by_name(job_gates(ex(clearance=Clearance(level="preferred")), run_date=RUN, max_age_days=30))
    assert g["clearance"].result == "pass" and "preferred" in g["clearance"].reason


def test_freshness_gate():
    def f(d, p=None):
        return by_name(job_gates(ex(postedDate=d), run_date=RUN, max_age_days=30, posted=p))["freshness"].result

    assert f(date(2026, 9, 1)) == "pass" and f(date(2026, 8, 31)) == "fail" and f(None) == "unknown"
    assert f(date(2026, 9, 25), p=date(2026, 7, 1)) == "fail"  # fetched JSON-LD date wins


def test_location_gate():
    us = [Location(kind="country", value="US")]

    def pg(locs, c, r):
        return by_name(profile_gates(ex(allowedLocations=locs), country=c, region=r))["location"].result

    assert pg(us, "BR", "Latam") == "fail" and pg(us, "US", "US") == "pass"
    assert pg([Location(kind="region", value="Americas")], "BR", None) == "pass"
    assert pg([Location(kind="region", value="EMEA")], "DE", "EU") == "pass"
    assert pg([Location(kind="region", value="Worldwide")], "IN", "Asia") == "pass"
    assert pg([Location(kind="region", value="North America")], "MX", "Latam") == "fail"
    assert pg([], "BR", "Latam") == "unknown" and pg(us, None, None) == "unknown"


def test_covers_country_in_profile_region():
    # A country entry matches the profile's region when the country belongs to that region.
    assert covers(Location(kind="country", value="DE"), None, "EU")
    assert not covers(Location(kind="country", value="DE"), None, "Latam")
    assert covers(Location(kind="region", value="latam"), "AR", None)


def test_work_auth_gate():
    g = profile_gates(ex(workAuthorization=WorkAuth(restricted=True, countries=["US"])), country="BR", region="Latam")
    assert by_name(g)["workAuth"].result == "fail"
    g = profile_gates(ex(workAuthorization=WorkAuth(restricted=True, countries=["US"])), country="US", region="US")
    assert by_name(g)["workAuth"].result == "pass"
    g = profile_gates(ex(workAuthorization=WorkAuth(restricted=True, countries=[])), country="BR", region="Latam")
    assert by_name(g)["workAuth"].result == "unknown"
    g = profile_gates(ex(workAuthorization=WorkAuth(restricted=True, countries=["US"])), country=None, region=None)
    assert by_name(g)["workAuth"].result == "unknown"
    assert by_name(profile_gates(ex(), country="BR", region=None))["workAuth"].result == "pass"


def test_blocked():
    assert blocked(job_gates(ex(workMode="hybrid"), run_date=RUN, max_age_days=30))
    assert not blocked(job_gates(ex(workMode="unknown"), run_date=RUN, max_age_days=30))


def test_unrecognised_locations_are_unknown_not_fail():
    def pg(locs):
        return by_name(profile_gates(ex(allowedLocations=locs), country="US", region="US"))["location"].result

    assert pg([Location(kind="region", value="Remote")]) == "unknown"
    assert pg([Location(kind="region", value="Mars")]) == "unknown"
    assert pg([Location(kind="region", value="Mars"), Location(kind="country", value="DE")]) == "fail"
    assert pg([Location(kind="region", value="Western Europe")]) == "fail"
    assert by_name(profile_gates(ex(allowedLocations=[Location(kind="region", value="Western Europe")]), country="DE", region="EU"))["location"].result == "pass"


def test_normalize_country_three_letter_codes():
    from app.job_apply.gates import normalize_country

    assert normalize_country("GBR") == "GB" and normalize_country("usa") == "US" and normalize_country("DEU") == "DE"
    assert normalize_country("BRA") == "BR" and normalize_country("POL") == "PL" and normalize_country("CAN") == "CA"
    assert normalize_country("XYZ") is None
