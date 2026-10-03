"""Screening (spec 2026-10-02): before profiles are chosen, every job gets one bucket saying whether it's worth
applying to and why not, and a location group; profiles are suggested per group by the same `covers()` rule the
location gate uses at scoring time, so the report and the results agree."""
from collections import Counter
from datetime import date
from typing import Any, Optional, get_args

from app.job_apply.gates import blocked, covers, failing_reasons, job_gates, normalize_country, region_key
from app.job_apply.schema import GateResult, Location
from app.models.job_apply import ScreenBucket

BUCKETS: tuple[str, ...] = get_args(ScreenBucket)
WORTH: tuple[str, ...] = ("valid", "check")
FORCEABLE: tuple[str, ...] = ("clearance", "onsite", "too_old")  # "Include anyway" can bring these back
NO_LOCATION = "none"

# region_key() result -> the label used in group keys. US / Canada are countries, so they get ISO-2 codes.
_REGION_LABEL = {
    "worldwide": "Worldwide", "anywhere": "Worldwide", "global": "Worldwide", "americas": "Americas",
    "north america": "North America", "latam": "Latam", "latin america": "Latam", "south america": "Latam",
    "emea": "EMEA", "europe": "Europe", "eu": "EU", "apac": "APAC", "asia": "Asia",
}
_REGION_COUNTRY = {"us": "US", "usa": "US", "united states": "US", "canada": "CA"}
_REGION_LABELS = frozenset(_REGION_LABEL.values())


def bucket(
    *, status: str, status_reason: Optional[str], gates: list[GateResult], group_key: Optional[str], force_include: bool
) -> Optional[ScreenBucket]:
    """The one bucket a job lands in, first match wins (spec §3.1). None while it is still being fetched or read."""
    if status in ("pending", "fetched"):
        return None
    if status == "fetch_failed":
        return "closed" if (status_reason or "").startswith("job closed") else "not_fetched"
    if status == "llm_failed":
        return "read_failed"
    result = {g.name: g.result for g in gates}
    for gate, name in (("clearance", "clearance"), ("remote", "onsite"), ("freshness", "too_old")):
        if result.get(gate) == "fail":
            return "check" if force_include else name
    if "unknown" in result.values() or group_key == NO_LOCATION:
        return "check"
    return "valid"


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


def profile_fits(key: str, *, country: Optional[str], region: Optional[str]) -> bool:
    """Every profile fits jobs with no stated location, and a profile with no location fits every group."""
    if key == NO_LOCATION or not (country or region):
        return True
    locs = [Location(kind="region" if p in _REGION_LABELS else "country", value=p) for p in key.split("+")]
    known = [loc for loc in locs if (region_key(loc.value) if loc.kind == "region" else normalize_country(loc.value))]
    if not known:  # the location gate reads these as "unknown", which doesn't block, so the report agrees
        return True
    return any(covers(loc, country, region) for loc in known)


def rescreen(row: Any, *, run_date: date, max_age_days: int) -> None:
    """Re-apply the job gates (max age changed, or Include anyway) to a row that has already been read."""
    if row.extraction is not None:
        row.gates = job_gates(row.extraction, run_date=run_date, max_age_days=max_age_days, posted=row.postedDate)
        if row.status in ("extracted", "excluded"):
            row.status = "excluded" if blocked(row.gates) else "extracted"
            row.statusReason = failing_reasons(row.gates) if row.status == "excluded" else None
    row.screen = bucket(
        status=row.status, status_reason=row.statusReason, gates=row.gates or [],
        group_key=row.groupKey, force_include=row.forceInclude,
    )


def build_report(rows: list[tuple[Optional[str], Optional[str]]], profiles: list[dict], *, max_age_days: int) -> dict:
    """`rows` are (screen, groupKey); `profiles` are {"id", "name", "country", "region", "resumes"}."""
    counts = Counter(screen for screen, _ in rows if screen)
    groups: dict[str, dict] = {}
    for screen, key in rows:
        if screen not in WORTH:
            continue
        g = groups.setdefault(key or NO_LOCATION, {"key": key or NO_LOCATION, "jobs": 0, "check": 0})
        g["jobs"] += 1
        g["check"] += screen == "check"
    ordered = sorted(groups.values(), key=lambda g: (-g["jobs"], g["key"]))
    for g in ordered:
        g["fits"] = [p["id"] for p in profiles if profile_fits(g["key"], country=p.get("country"), region=p.get("region"))]
    return {
        "total": len(rows),
        "worth": counts["valid"] + counts["check"],
        "check": counts["check"],
        "buckets": {b: counts[b] for b in BUCKETS},
        "maxAgeDays": max_age_days,
        "groups": ordered,
        "profiles": profiles,
    }
