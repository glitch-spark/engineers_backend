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


def test_region_rules():
    def pg(kind, value, c, r):
        g = profile_gates(ex(allowedLocations=[Location(kind=kind, value=value)]), country=c, region=r)
        return by_name(g)["location"].result

    cases = [
        # (kind, value, profile country, profile region, expected)
        ("region", "Worldwide", "AU", None, "pass"),
        ("region", "anywhere", "BR", "Latam", "pass"),
        ("region", "LATAM", "BR", "Latam", "pass"),
        ("region", "Latin America", "MX", "Latam", "pass"),
        ("region", "LATAM & Caribbean", "CO", "Latam", "pass"),
        ("region", "LatAm", "US", "US", "fail"),
        ("region", "South America", "BR", "Latam", "pass"),
        ("region", "South America", "AR", "Latam", "pass"),
        ("region", "South America", "MX", "Latam", "fail"),
        ("region", "North America", "CA", None, "pass"),
        ("region", "North America", "MX", "Latam", "fail"),
        ("region", "America", "BR", "Latam", "pass"),
        ("region", "the Americas", "US", "US", "pass"),
        ("region", "Americas", "CA", None, "pass"),
        ("region", "Eu", "GB", "EU", "pass"),
        ("region", "Europe", "IE", "EU", "pass"),
        ("region", "EU", "NL", "EU", "pass"),
        ("region", "European Union", "GB", "EU", "fail"),
        ("region", "European Union", "IE", "EU", "pass"),
        ("region", "European Union", "FI", "EU", "pass"),
        ("region", "UK", "GB", "EU", "pass"),
        ("region", "UK", "IE", None, "pass"),
        ("region", "UK", "NL", "EU", "fail"),
        ("region", "EMEA", "ZA", None, "pass"),
        ("region", "EMEA", "NL", "EU", "pass"),
        ("region", "EMEA", "US", "US", "fail"),
        ("country", "GB", "IE", "EU", "pass"),
        ("country", "IE", "GB", "EU", "pass"),
        ("country", "GB", "NL", "EU", "fail"),
        ("country", "MX", "BR", "Latam", "fail"),  # a single country stays that country
        ("country", "BR", "BR", None, "pass"),
        ("region", "South America", None, "Latam", "pass"),  # region-only profile: overlap is enough
        ("country", "BR", None, "Latam", "pass"),
    ]
    for kind, value, c, r, want in cases:
        assert pg(kind, value, c, r) == want, (kind, value, c, r)


def test_america_is_a_region():
    from app.job_apply.gates import normalize_country, region_key

    assert normalize_country("America") is None and region_key("America") == "americas"
    assert region_key("Ukraine") is None and region_key("UK & Ireland") == "uk"
    assert region_key("European Union") == "european union" and region_key("Western EU") == "europe"


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


def test_remote_only_in_some_areas_needs_a_check():
    # "Remote - Austin Metro" / "Remote - North Carolina": profiles have no city or state, so a person decides
    g = by_name(job_gates(ex(workMode="remote", remoteAreas=["Austin Metro", "North Carolina"]), run_date=RUN, max_age_days=30))
    assert g["area"].result == "unknown" and g["area"].reason == "remote only in Austin Metro, North Carolina"
    assert not blocked(list(g.values()))
    assert "area" not in by_name(job_gates(ex(workMode="remote"), run_date=RUN, max_age_days=30))
