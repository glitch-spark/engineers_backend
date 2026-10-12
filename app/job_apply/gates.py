"""Eligibility gates (spec §3.7). Gates decide eligibility and never change a score.

Job-level gates (remote, clearance, freshness) apply to every profile; profile gates (location,
workAuth) compare the posting with one profile's country/region. `unknown` counts as a pass.
"""
import re
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
# Profile region (Account.region) -> the countries it stands for.
_PROFILE_REGION_COUNTRIES: dict[str, frozenset[str]] = {r: frozenset(cs.split()) for r, cs in _REGION_COUNTRIES.items()}

_LATAM = _PROFILE_REGION_COUNTRIES["Latam"]
_EUROPE = _PROFILE_REGION_COUNTRIES["EU"]
_ASIA = _PROFILE_REGION_COUNTRIES["Asia"]
_SOUTH_AMERICA = frozenset("AR BO BR CL CO EC GY PY PE SR UY VE".split())
_AFRICA = frozenset(
    "DZ AO BJ BW BF BI CV CM CF TD KM CG CD CI DJ EG GQ ER SZ ET GA GM GH GN GW KE LS LR LY MG MW ML MR MU MA MZ NA "
    "NE NG RW ST SN SC SL SO ZA SS SD TZ TG TN UG ZM ZW".split()
)
_UK_IE = frozenset({"GB", "UK", "IE"})
_NORTH_AMERICA = frozenset({"US", "CA"})

# Region named in a posting -> the profile countries it admits; None admits everyone.
_REGION_COVER: dict[str, Optional[frozenset[str]]] = {
    "worldwide": None,
    "anywhere": None,
    "global": None,
    "americas": _NORTH_AMERICA | _LATAM,  # also "America" / "the Americas" (see _REGION_NEEDLES)
    "north america": _NORTH_AMERICA,
    "latam": _LATAM,
    "latin america": _LATAM,
    "south america": _SOUTH_AMERICA,
    "europe": _EUROPE,
    "eu": _EUROPE,
    "european union": _EUROPE - {"GB", "UK"},
    "emea": _EUROPE | _AFRICA,
    "africa": _AFRICA,
    "apac": _ASIA,
    "asia": _ASIA,
    "us": frozenset({"US"}),
    "usa": frozenset({"US"}),
    "united states": frozenset({"US"}),
    "canada": frozenset({"CA"}),
    "uk": _UK_IE,
    "united kingdom": _UK_IE,
    "ireland": _UK_IE,
}
# A country named in a posting also admits these neighbours.
_SAME_COUNTRY: dict[str, frozenset[str]] = {"GB": _UK_IE, "IE": _UK_IE}


_COUNTRY_NAMES = {
    "united states": "US", "united states of america": "US", "usa": "US", "u.s.": "US", "u.s.a.": "US", "u.s": "US",
    "canada": "CA", "united kingdom": "GB", "uk": "GB", "u.k.": "GB", "great britain": "GB",
    "england": "GB", "scotland": "GB", "britain": "GB", "ireland": "IE", "germany": "DE", "france": "FR",
    "spain": "ES", "portugal": "PT", "italy": "IT", "netherlands": "NL", "the netherlands": "NL", "belgium": "BE",
    "poland": "PL", "sweden": "SE", "norway": "NO", "denmark": "DK", "finland": "FI", "switzerland": "CH",
    "austria": "AT", "czech republic": "CZ", "czechia": "CZ", "romania": "RO", "ukraine": "UA", "greece": "GR",
    "hungary": "HU", "serbia": "RS", "croatia": "HR", "bulgaria": "BG", "turkey": "TR", "brazil": "BR",
    "mexico": "MX", "argentina": "AR", "colombia": "CO", "chile": "CL", "peru": "PE", "uruguay": "UY",
    "paraguay": "PY", "bolivia": "BO", "ecuador": "EC", "venezuela": "VE", "costa rica": "CR", "panama": "PA",
    "guatemala": "GT", "dominican republic": "DO", "puerto rico": "PR", "india": "IN", "pakistan": "PK",
    "bangladesh": "BD", "sri lanka": "LK", "philippines": "PH", "vietnam": "VN", "indonesia": "ID",
    "malaysia": "MY", "singapore": "SG", "thailand": "TH", "china": "CN", "japan": "JP", "south korea": "KR",
    "korea": "KR", "taiwan": "TW", "hong kong": "HK", "australia": "AU", "new zealand": "NZ",
}


# ISO 3166 alpha-3 codes some boards and models use ("GBR", "DEU"): the countries the regions above cover, plus a
# few common elsewhere.
_ALPHA3 = {
    "USA": "US", "CAN": "CA", "GBR": "GB", "IRL": "IE", "DEU": "DE", "FRA": "FR", "ESP": "ES", "PRT": "PT",
    "ITA": "IT", "NLD": "NL", "BEL": "BE", "LUX": "LU", "AUT": "AT", "CHE": "CH", "DNK": "DK", "SWE": "SE",
    "NOR": "NO", "FIN": "FI", "ISL": "IS", "POL": "PL", "CZE": "CZ", "SVK": "SK", "HUN": "HU", "ROU": "RO",
    "BGR": "BG", "GRC": "GR", "HRV": "HR", "SVN": "SI", "SRB": "RS", "BIH": "BA", "MNE": "ME", "MKD": "MK",
    "ALB": "AL", "MDA": "MD", "UKR": "UA", "BLR": "BY", "EST": "EE", "LVA": "LV", "LTU": "LT", "MLT": "MT",
    "CYP": "CY", "TUR": "TR", "MEX": "MX", "BRA": "BR", "ARG": "AR", "COL": "CO", "CHL": "CL", "PER": "PE",
    "URY": "UY", "PRY": "PY", "BOL": "BO", "ECU": "EC", "VEN": "VE", "CRI": "CR", "PAN": "PA", "GTM": "GT",
    "HND": "HN", "SLV": "SV", "NIC": "NI", "DOM": "DO", "CUB": "CU", "PRI": "PR", "IND": "IN", "PAK": "PK",
    "BGD": "BD", "LKA": "LK", "PHL": "PH", "VNM": "VN", "IDN": "ID", "MYS": "MY", "SGP": "SG", "THA": "TH",
    "CHN": "CN", "JPN": "JP", "KOR": "KR", "TWN": "TW", "HKG": "HK", "AUS": "AU", "NZL": "NZ", "ISR": "IL",
    "ARE": "AE", "SAU": "SA", "ZAF": "ZA", "EGY": "EG", "NGA": "NG", "KEN": "KE", "MAR": "MA",
}


def normalize_country(value: str) -> Optional[str]:
    """'United States' / 'USA' / 'us' / 'GBR' -> ISO-2. None when the value isn't a recognisable country."""
    v = (value or "").strip()
    if len(v) == 2 and v.isalpha():
        return "GB" if v.upper() == "UK" else v.upper()
    return (
        _COUNTRY_NAMES.get(v.lower().rstrip(".")) or _COUNTRY_NAMES.get(v.lower())
        or (_ALPHA3.get(v.upper()) if len(v) == 3 and v.isalpha() else None)
    )


_REGION_NEEDLES = (
    ("worldwide", "worldwide"), ("anywhere", "anywhere"), ("global", "global"), ("latin america", "latam"),
    ("latam", "latam"), ("south america", "south america"), ("north america", "north america"),
    ("america", "americas"), ("emea", "emea"), ("european union", "european union"), ("europe", "europe"),
    ("united kingdom", "uk"), ("ireland", "uk"), ("africa", "africa"), ("apac", "apac"), ("asia", "asia"),
)


def region_key(value: str) -> Optional[str]:
    """Map a free-form region name onto a key of _REGION_COVER, or None when it isn't recognised."""
    v = " ".join((value or "").lower().replace("-", " ").split())
    if v in _REGION_COVER:
        return v
    if "european union" in v:
        return "european union"
    if re.search(r"\beu\b", v):  # "Western EU", "EU only"
        return "europe"
    if re.search(r"\buk\b", v):  # "UK & Ireland", "UK only" (not "Ukraine")
        return "uk"
    # A time-zone window given as the location ("CET +/-2", "GMT±3", "EST hours") means the region around it.
    if re.search(r"\b(?:gmt|cet|cest|bst|wet|eet|eest)\b", v):
        return "emea"
    if re.search(r"\b(?:est|edt|cst|cdt|mst|mdt|pst|pdt|et|pt)\b", v):
        return "north america"
    return next((key for needle, key in _REGION_NEEDLES if needle in v), None)


def _recognised(loc: Location) -> bool:
    if loc.kind == "region":
        return region_key(loc.value) is not None
    return normalize_country(loc.value) is not None


def covers(loc: Location, country: Optional[str], region: Optional[str]) -> bool:
    """Does one allowed-location entry admit a profile living in `country` / `region`? The profile's country decides
    when it has one; a profile with only a region matches when the entry admits any of that region's countries."""
    country = (country or "").upper() or None
    if country == "UK":
        country = "GB"
    region_countries = _PROFILE_REGION_COUNTRIES.get(region or "", frozenset())
    if loc.kind == "region":
        key = region_key(loc.value)
        if key is None:
            return False
        admitted = _REGION_COVER[key]
        if admitted is None:
            return bool(country or region)
        return country in admitted if country else bool(region_countries & admitted)
    value = normalize_country(loc.value)
    if value is None:
        return False
    if country:
        return country in _SAME_COUNTRY.get(value, frozenset({value}))
    # Profile has only a region: a country entry counts when that country lies in the region.
    return value in region_countries


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
    gates = [remote, clearance, fresh]
    if ex.remoteAreas:  # profiles have a country / region only, so a person checks the city or state
        gates.append(GateResult(name="area", result="unknown", reason=f"remote only in {', '.join(ex.remoteAreas)}"))
    return gates


def profile_gates(ex: JobExtraction, *, country: Optional[str], region: Optional[str]) -> list[GateResult]:
    has_location = bool(country or region)
    where = country or region or ""

    known = [l for l in ex.allowedLocations if _recognised(l)]
    if not ex.allowedLocations:
        location = GateResult(name="location", result="unknown", reason="location not stated")
    elif not known:
        location = GateResult(
            name="location", result="unknown", reason=f"location not understood: {_describe(ex.allowedLocations)}"
        )
    elif not has_location:
        location = GateResult(name="location", result="unknown", reason="profile has no country or region")
    elif any(covers(l, country, region) for l in known):
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


def failing_reasons(gates: list[GateResult]) -> str:
    return "; ".join(f"{g.name}: {g.reason}" if g.reason else g.name for g in gates if g.result == "fail")
