"""Eligibility gates (spec §3.7). Gates decide eligibility and never change a score.

Job-level gates (remote, clearance, freshness) apply to every profile; profile gates (location,
workAuth) compare the posting with one profile's country/region. `unknown` counts as a pass.
"""
from datetime import date
from typing import Optional

from app.job_apply.schema import GateResult, JobExtraction, Location

_REGION_COUNTRIES = {
    "Latam": "MX BR AR CO CL PE UY PY BO EC VE CR PA GT HN SV NI DO CU PR",
    "EU": "AT BE BG HR CY CZ DK EE FI FR DE GR HU IE IT LV LT LU MT NL PL PT RO SK SI ES SE GB UK CH NO IS UA RS BA "
          "ME MK AL MD BY TR",
    "Canada": "CA",
    "US": "US",
    "Asia": "IN PK BD LK NP PH VN ID MY SG TH CN JP KR TW HK KH MM LA MN KZ UZ",
}
COUNTRY_REGION: dict[str, str] = {c: region for region, cs in _REGION_COUNTRIES.items() for c in cs.split()}

_ALL = frozenset(_REGION_COUNTRIES)
_REGION_COVER: dict[str, frozenset[str]] = {
    "worldwide": _ALL,
    "anywhere": _ALL,
    "global": _ALL,
    "americas": frozenset({"US", "Canada", "Latam"}),
    "north america": frozenset({"US", "Canada"}),
    "latam": frozenset({"Latam"}),
    "latin america": frozenset({"Latam"}),
    "south america": frozenset({"Latam"}),
    "emea": frozenset({"EU"}),
    "europe": frozenset({"EU"}),
    "eu": frozenset({"EU"}),
    "apac": frozenset({"Asia"}),
    "asia": frozenset({"Asia"}),
    "us": frozenset({"US"}),
    "usa": frozenset({"US"}),
    "united states": frozenset({"US"}),
    "canada": frozenset({"Canada"}),
}


def _profile_region(country: Optional[str], region: Optional[str]) -> Optional[str]:
    return region or COUNTRY_REGION.get((country or "").upper())


def covers(loc: Location, country: Optional[str], region: Optional[str]) -> bool:
    """Does one allowed-location entry admit a profile living in `country` / `region`?"""
    country = (country or "").upper() or None
    prof_region = _profile_region(country, region)
    if loc.kind == "region":
        allowed = _REGION_COVER.get(loc.value.strip().lower(), frozenset())
        return prof_region in allowed
    value = loc.value.strip().upper()
    if country:
        return value == country or (value == "UK" and country == "GB") or (value == "GB" and country == "UK")
    # Profile has only a region: a country entry counts when that country lies in the region.
    return prof_region is not None and COUNTRY_REGION.get(value) == prof_region


def _describe(locs: list[Location]) -> str:
    return ", ".join(l.value for l in locs)


def job_gates(
    ex: JobExtraction, *, run_date: date, max_age_days: int, posted: Optional[date] = None
) -> list[GateResult]:
    if ex.workMode == "remote":
        remote = GateResult(name="remote", result="pass", reason="remote")
    elif ex.workMode in ("hybrid", "onsite"):
        remote = GateResult(name="remote", result="fail", reason=ex.workMode)
    else:
        remote = GateResult(name="remote", result="unknown", reason="work mode not stated")

    note = f" ({ex.clearance.note})" if ex.clearance.note else ""
    if ex.clearance.level == "required":
        clearance = GateResult(name="clearance", result="fail", reason=f"security clearance required{note}")
    elif ex.clearance.level == "preferred":
        clearance = GateResult(name="clearance", result="pass", reason=f"clearance preferred{note}")
    else:
        clearance = GateResult(name="clearance", result="pass")

    when = posted or ex.postedDate
    if when is None:
        fresh = GateResult(name="freshness", result="unknown", reason="date unknown")
    else:
        age = (run_date - when).days
        if age > max_age_days:
            fresh = GateResult(name="freshness", result="fail", reason=f"posted {age} days ago")
        else:
            fresh = GateResult(name="freshness", result="pass", reason=f"posted {max(age, 0)} days ago")
    return [remote, clearance, fresh]


def profile_gates(ex: JobExtraction, *, country: Optional[str], region: Optional[str]) -> list[GateResult]:
    has_location = bool(country or region)
    where = country or region or ""

    if not ex.allowedLocations:
        location = GateResult(name="location", result="unknown", reason="location not stated")
    elif not has_location:
        location = GateResult(name="location", result="unknown", reason="profile has no country or region")
    elif any(covers(l, country, region) for l in ex.allowedLocations):
        location = GateResult(name="location", result="pass", reason=_describe(ex.allowedLocations))
    else:
        location = GateResult(
            name="location", result="fail", reason=f"allows {_describe(ex.allowedLocations)}; profile is {where}"
        )

    wa = ex.workAuthorization
    if wa is None or not wa.restricted:
        auth = GateResult(name="workAuth", result="pass")
    elif not wa.countries:
        auth = GateResult(name="workAuth", result="unknown", reason=wa.note or "work authorization restricted")
    elif not country:
        auth = GateResult(name="workAuth", result="unknown", reason="profile has no country")
    elif country.upper() in wa.countries:
        auth = GateResult(name="workAuth", result="pass", reason=wa.note or "")
    else:
        auth = GateResult(
            name="workAuth", result="fail", reason=wa.note or f"work authorization limited to {', '.join(wa.countries)}"
        )
    return [location, auth]


def blocked(gates: list[GateResult]) -> bool:
    return any(g.result == "fail" for g in gates)
