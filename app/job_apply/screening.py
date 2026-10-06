"""Screening (spec 2026-10-02): before profiles are chosen, every job gets one bucket saying whether it's worth
applying to and why not, and a location group (where candidates may live). The report sorts worthwhile jobs into
the three target candidate markets (US, UK/EU, Latam), suggests each market's profiles, and only counts jobs open
to candidates elsewhere ("other locations")."""
from collections import Counter
from datetime import date
from typing import Any, Optional, get_args

from app.job_apply.gates import (
    COUNTRY_REGION,
    blocked,
    failing_reasons,
    job_gates,
    normalize_country,
    region_key,
)
from app.job_apply.schema import GateResult, Location
from app.models.job_apply import ScreenBucket

BUCKETS: tuple[str, ...] = get_args(ScreenBucket)
WORTH: tuple[str, ...] = ("valid", "check")
# A person can approve a job in these buckets: it becomes Worth applying. The others (closed, not fetched, couldn't be
# read, not a job page) have no readable description to score or tailor from: it has to be pasted in Job info first.
APPROVABLE: tuple[str, ...] = ("check", "clearance", "onsite", "too_old", "other_location")
NO_LOCATION = "none"

# region_key() result -> the label used in group keys. US / Canada are countries, so they get ISO-2 codes.
_REGION_LABEL = {
    "worldwide": "Worldwide", "anywhere": "Worldwide", "global": "Worldwide", "americas": "Americas",
    "north america": "North America", "latam": "Latam", "latin america": "Latam", "south america": "Latam",
    "emea": "EMEA", "europe": "Europe", "eu": "EU", "apac": "APAC", "asia": "Asia",
}
_REGION_COUNTRY = {"us": "US", "usa": "US", "united states": "US", "canada": "CA"}

# Target candidate markets, in display order. A job is in every market whose candidates it accepts.
MARKETS: tuple[str, ...] = ("US", "UKEU", "LATAM")
_LABEL_MARKETS = {
    "Worldwide": MARKETS, "Americas": ("US", "LATAM"), "North America": ("US",), "Latam": ("LATAM",),
    "EMEA": ("UKEU",), "Europe": ("UKEU",), "EU": ("UKEU",),
}
_PROFILE_REGION_MARKET = {"US": "US", "EU": "UKEU", "Latam": "LATAM"}  # Account.region -> market


def markets_for(key: Optional[str]) -> list[str]:
    """Target markets whose candidates a job with this location group accepts (in MARKETS order)."""
    found: set[str] = set()
    for part in (key or NO_LOCATION).split("+"):
        if part in _LABEL_MARKETS:
            found.update(_LABEL_MARKETS[part])
        elif len(part) == 2 and part.isupper():
            market = _PROFILE_REGION_MARKET.get(COUNTRY_REGION.get(part, ""))
            if market:
                found.add(market)
    return [m for m in MARKETS if m in found]


def profile_market(*, country: Optional[str], region: Optional[str]) -> Optional[str]:
    """The target market a profile's candidate lives in; None when outside the three (or not set)."""
    return _PROFILE_REGION_MARKET.get(region or "") or _PROFILE_REGION_MARKET.get(COUNTRY_REGION.get((country or "").upper(), ""))


def bucket(
    *,
    status: str,
    status_reason: Optional[str],
    gates: list[GateResult],
    group_key: Optional[str],
    force_include: bool,
    is_job: bool = True,
    verified: bool = True,
) -> Optional[ScreenBucket]:
    """The one bucket a job lands in, first match wins (spec §3.1). None while it is still being fetched or read.
    `force_include` is a person's approval: a job that has been read (and is a job posting) is then Worth applying,
    whatever the checks said."""
    if status in ("pending", "fetched"):
        return None
    if status == "fetch_failed":
        return "closed" if (status_reason or "").startswith("job closed") else "not_fetched"
    if status == "llm_failed":
        return "read_failed"
    if not is_job:
        return "not_job"  # an error page, cookie notice or job list read in place of a posting
    if force_include:
        return "valid"
    result = {g.name: g.result for g in gates}
    for gate, name in (("clearance", "clearance"), ("remote", "onsite"), ("freshness", "too_old")):
        if result.get(gate) == "fail":
            return name
    if group_key and group_key != NO_LOCATION and not markets_for(group_key):
        return "other_location"  # open only to candidates outside the target markets: counted, not scored
    if "unknown" in result.values() or group_key == NO_LOCATION or not verified:
        return "check"  # an unknown, or read from page text only (no job-site API or JobPosting data)
    return "valid"


def scoring_key(group_key: Optional[str], approved: bool) -> Optional[str]:
    """The location group a job is scored and reported under. An approved job open only outside the target markets
    has no market of its own, so it is handled like one that states no location (every profile fits)."""
    if approved and group_key and group_key != NO_LOCATION and not markets_for(group_key):
        return NO_LOCATION
    return group_key


def _part(loc: Location) -> str:
    value = (loc.value or "").strip()
    if loc.kind == "country" and (code := normalize_country(value)):
        return code
    key = region_key(value)
    if key in _REGION_COUNTRY:
        return _REGION_COUNTRY[key]
    if key in _REGION_LABEL:
        return _REGION_LABEL[key]
    return normalize_country(value) or value


def group_key(locations: list[Location]) -> str:
    """Normalised allowed locations, de-duplicated and sorted: "GB", "EU+US", "Worldwide"; "none" when empty."""
    parts = sorted({p for p in (_part(loc) for loc in locations) if p})
    return "+".join(parts) if parts else NO_LOCATION


def rescreen(row: Any, *, run_date: date, max_age_days: int) -> None:
    """Re-apply the job gates (max age changed) to a row that has already been read; an approval stays."""
    if row.extraction is not None:
        row.gates = job_gates(row.extraction, run_date=run_date, max_age_days=max_age_days, posted=row.postedDate)
        if row.status in ("extracted", "excluded"):
            row.status = "excluded" if blocked(row.gates) else "extracted"
            row.statusReason = failing_reasons(row.gates) if row.status == "excluded" else None
    row.screen = bucket(
        status=row.status, status_reason=row.statusReason, gates=row.gates or [],
        group_key=row.groupKey, force_include=row.forceInclude,
        is_job=row.extraction.isJobPosting if row.extraction is not None else True,
        verified=row.jdStructured is not False,
    )


def build_report(rows: list[tuple[Optional[str], Optional[str]]], profiles: list[dict], *, max_age_days: int) -> dict:
    """`rows` are (screen, groupKey); `profiles` are {"id", "name", "country", "region", "resumes"}.

    `groups`: the three markets (always shown) and, when there are any, jobs with no stated location; each lists the
    profiles whose candidate market it is (a profile with no location fits all). `others`: job counts per location
    for jobs open only outside the markets."""
    counts = Counter(screen for screen, _ in rows if screen)
    groups = {k: {"key": k, "jobs": 0, "check": 0} for k in (*MARKETS, NO_LOCATION)}
    combos: dict[tuple, dict] = {}
    others: Counter = Counter()
    for screen, key in rows:
        if screen == "other_location":
            others[key or NO_LOCATION] += 1
        if screen not in WORTH:
            continue
        keys = markets_for(key) or ([NO_LOCATION] if (key or NO_LOCATION) == NO_LOCATION else [])
        for k in keys:
            groups[k]["jobs"] += 1
            groups[k]["check"] += screen == "check"
        if keys:
            c = combos.setdefault(tuple(keys), {"markets": list(keys), "jobs": 0, "check": 0})
            c["jobs"] += 1
            c["check"] += screen == "check"
    shown = [groups[m] for m in MARKETS] + ([groups[NO_LOCATION]] if groups[NO_LOCATION]["jobs"] else [])
    for g in shown:
        g["fits"] = [
            p["id"] for p in profiles
            if g["key"] == NO_LOCATION
            or profile_market(country=p.get("country"), region=p.get("region")) in (g["key"], None)
        ]
    return {
        "total": len(rows),
        "worth": counts["valid"] + counts["check"],
        "check": counts["check"],
        "buckets": {b: counts[b] for b in BUCKETS},
        "maxAgeDays": max_age_days,
        "groups": shown,
        "others": [{"key": k, "jobs": n} for k, n in sorted(others.items(), key=lambda kv: (-kv[1], kv[0]))],
        # Each worthwhile job once, by its set of markets: a profile picked in two of them still counts it once.
        "combos": sorted(combos.values(), key=lambda c: (c["markets"] == [NO_LOCATION], len(c["markets"]), c["markets"])),
        "profiles": profiles,
    }
