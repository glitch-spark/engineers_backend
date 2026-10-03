"""The per-run "Checks" tab of the Google Sheet export: every job with its posting date, location, clearance, work
mode and status (CHECKS_HEADER). Works on anything shaped like a JobApplyRow."""
import re
from typing import Any

CHECKS_HEADER = [
    "Company Name", "Job URL", "Job Title", "Posting Date", "Location", "Security Clearance",
    "Remote/Hybrid/Onsite", "Status",
]

STATUS_LABEL = {  # same words as the app's screening report
    "valid": "Worth applying", "check": "Needs a check", "closed": "Closed", "not_fetched": "Not fetched",
    "read_failed": "Couldn't read", "not_job": "Not a job page", "clearance": "Clearance required",
    "onsite": "On-site / hybrid", "too_old": "Too old", "other_location": "Other locations",
}
WORK_MODE = {"remote": "Remote", "hybrid": "Hybrid", "onsite": "On-site", "unknown": "Not stated"}


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


def _location(row: Any) -> str:
    """Where candidates may live: "US", "US (Austin Metro; North Carolina)" when remote is limited to some areas."""
    if row.extraction is None:
        return ""
    where = ", ".join(loc.value for loc in row.extraction.allowedLocations) or "Not stated"
    areas = getattr(row.extraction, "remoteAreas", None) or []
    return f"{where} ({'; '.join(areas)})" if areas else where


def _clearance(row: Any) -> str:
    if row.extraction is None:
        return ""
    c = row.extraction.clearance
    if c.level == "none":
        return "None"
    label = "Required" if c.level == "required" else "Preferred"
    return f"{label}: {c.note}" if c.note else label


def _work_mode(row: Any) -> str:
    return WORK_MODE.get(row.extraction.workMode, row.extraction.workMode) if row.extraction is not None else ""


def checks_row(row: Any) -> list[Any]:
    """One line of the Checks tab."""
    ex = row.extraction
    cells = [
        row.company or (ex.company if ex else ""), row.url, row.title or (ex.title if ex else ""),
        row.postedDate.isoformat() if row.postedDate else "", _location(row), _clearance(row), _work_mode(row),
        STATUS_LABEL.get(row.screen or "", "Not checked yet"),
    ]
    return [sheet_text(c) for c in cells]
