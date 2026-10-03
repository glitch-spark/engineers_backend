"""What the checks found, as Google Sheet cells, so a person can validate them: four columns on each exported
application row (APPLY_HEADER) and a per-run "Checks" tab listing every job with its result and why
(CHECKS_HEADER). Works on anything shaped like a JobApplyRow."""
import re
from typing import Any, Optional

from app.job_apply.schema import GateResult
from app.job_apply.screening import MARKETS, NO_LOCATION, markets_for

CHECKS_HEADER = [
    "Sheet row", "Company", "Title", "Job URL", "Result", "Reason", "Posted", "Posted from", "Location",
    "Market", "Work mode", "Clearance", "Checks", "Included anyway", "Top score",
]
APPLY_HEADER = ["Location", "Work mode", "Clearance", "Checks"]

RESULT_LABEL = {  # same words as the app's screening report
    "valid": "Worth applying", "check": "Needs a check", "closed": "Closed", "not_fetched": "Not fetched",
    "read_failed": "Couldn't read", "not_job": "Not a job page", "clearance": "Clearance required",
    "onsite": "On-site / hybrid", "too_old": "Too old", "other_location": "Other locations",
}
MARKET_LABEL = {"US": "US", "UKEU": "UK / EU", "LATAM": "Latam"}
WORK_MODE = {"remote": "Remote", "hybrid": "Hybrid", "onsite": "On-site", "unknown": "Not stated"}
MARK = {"pass": "✓", "fail": "✗", "unknown": "?"}
JOB_GATES = ("freshness", "remote", "clearance")  # shown in this order
PAGE_TEXT_ONLY = "read from page text only"


def sheet_text(value: Any) -> Any:
    """Text for a USER_ENTERED cell: a leading ' keeps a value that starts like a formula (titles and companies come
    from job pages and the user's sheet) from being evaluated."""
    if isinstance(value, str) and value and value[0] in "=+-@\t\r":
        return "'" + value
    return value


def checks_tab_name(run: Any) -> str:
    """One tab per run: "Checks · <sheet name> · <last 6 of the run id>" (Sheets allows 100 characters)."""
    name = re.sub(r"[\[\]:/\\?*']", "", run.fileName or "").strip()[:60]
    return f"Checks · {name} · {str(run.id)[-6:]}"


def posted_from(row: Any) -> str:
    if row.fetchedPostedDate:
        return "job page data"  # job-site API or JobPosting data
    if row.postedDate:
        return "read from page text"
    return "unknown"


def _location(row: Any) -> str:
    if row.extraction is None:
        return ""
    return ", ".join(loc.value for loc in row.extraction.allowedLocations) or "Not stated"


def _market(row: Any) -> str:
    if not row.groupKey:
        return ""
    if row.groupKey == NO_LOCATION:
        return "Not stated"
    found = markets_for(row.groupKey)
    return " + ".join(MARKET_LABEL[m] for m in MARKETS if m in found) or f"Other ({row.groupKey})"


def _work_mode(row: Any) -> str:
    return WORK_MODE.get(row.extraction.workMode, row.extraction.workMode) if row.extraction is not None else ""


def _clearance(row: Any) -> str:
    if row.extraction is None:
        return ""
    c = row.extraction.clearance
    if c.level == "none":
        return "None"
    label = "Required" if c.level == "required" else "Preferred"
    return f"{label}: {c.note}" if c.note else label


def _gate(g: GateResult, source: str) -> str:
    m = MARK.get(g.result, "?")
    if g.name == "freshness":
        return f"{m} {g.reason or 'date unknown'}" + (f" ({source})" if g.result != "unknown" else "")
    if g.name == "clearance":
        return f"{m} {g.reason or 'no clearance'}"
    if g.name == "remote" and g.result == "pass":
        return f"{m} remote"
    label = {"workAuth": "work auth"}.get(g.name, g.name)
    return f"{m} {label}: {g.reason}" if g.reason else f"{m} {label}"


def _job_checks(row: Any) -> list[str]:
    source = posted_from(row)
    by_name = {g.name: g for g in row.gates or []}
    out = [_gate(by_name[n], source) for n in JOB_GATES if n in by_name]
    if row.extraction is not None and not row.extraction.isJobPosting:
        out.append("✗ not a job page")
    if row.extraction is not None and row.jdStructured is False:
        out.append(f"? {PAGE_TEXT_ONLY}")
    return out


def _profile_checks(row: Any, account_id: str) -> list[str]:
    mine = next((p for p in row.profileGates or [] if p.accountId == account_id), None)
    return [_gate(g, "") for g in mine.gates] if mine else []


def _reason(row: Any) -> str:
    if row.statusReason:
        return row.statusReason
    if row.screen == "not_job":
        return "not a job posting (error page, job list or careers page)"
    if row.screen == "other_location":
        return f"open to candidates in {_location(row)}"
    if row.screen == "check":
        why = [g.reason for g in row.gates or [] if g.result == "unknown" and g.reason]
        if row.groupKey == NO_LOCATION:
            why.append("location not stated")
        if row.jdStructured is False:
            why.append(PAGE_TEXT_ONLY)
        if row.forceInclude:
            why.append("included anyway")
        return "; ".join(why)
    return ""


def checks_row(row: Any, names: dict[str, str]) -> list[Any]:
    """One line of the Checks tab. `names`: profile id -> name, for the per-profile checks of scored jobs."""
    checks = " · ".join(_job_checks(row))
    for p in row.profileGates or []:
        mine = _profile_checks(row, p.accountId)
        if mine:
            checks += f" | {names.get(p.accountId, 'Profile')}: " + " · ".join(mine)
    result = RESULT_LABEL.get(row.screen or "", "Not checked yet")
    ex = row.extraction
    cells = [
        row.rowIndex, row.company or (ex.company if ex else ""), row.title or (ex.title if ex else ""), row.url,
        result, _reason(row), row.postedDate.isoformat() if row.postedDate else "",
        posted_from(row) if row.extraction is not None or row.postedDate else "",
        _location(row), _market(row), _work_mode(row), _clearance(row), checks,
        "Yes" if row.forceInclude else "", row.topScore if row.topScore is not None else "",
    ]
    return [sheet_text(c) for c in cells]


def apply_cells(row: Any, account_id: str) -> list[Any]:
    """Location, Work mode, Clearance and Checks (the job's, then this profile's) for one exported application."""
    checks = " · ".join(_job_checks(row) + _profile_checks(row, account_id))
    return [sheet_text(c) for c in (_location(row), _work_mode(row), _clearance(row), checks)]
