"""Point an exported Google Sheet row at a re-tailored resume.

The export (POST /runs/{id}/export-sheet) records where each row went (JobApplyRow.exportedRows). When a newer
tailored resume completes for a job and profile already in the sheet, its download link replaces the old one in
that row — after checking the row still shows the same profile and job; if the tab was sorted or edited, the row
is found again by Profile + Job URL, and if it's gone the sheet is left alone. A Sheets problem never fails the
resume job: it is logged and skipped.
"""
import logging
from typing import Optional

import httpx
from beanie import PydanticObjectId

from app import google_sheets
from app.job_apply import file_links

log = logging.getLogger(__name__)

_PROFILE, _URL = 0, 3  # columns A (Profile) and D (Job URL) of the export; E is Download Resume


def _matches(cells: list[str], url: str, profile: str) -> bool:
    return len(cells) > _URL and cells[_URL] == url and cells[_PROFILE] == profile


def find_row(values: list[list[str]], *, recorded: int, url: str, profile: str) -> Optional[int]:
    """Sheet row number (1-based; `values` start at row 1) holding this job + profile: the recorded row when it
    still matches, else the first row that does, else None."""
    if 1 <= recorded <= len(values) and _matches(values[recorded - 1], url, profile):
        return recorded
    return next((i + 1 for i, cells in enumerate(values) if _matches(cells, url, profile)), None)


async def fill_link(job) -> bool:
    """Write `job`'s download link into its exported row. True when written."""
    from app.models import Account
    from app.models.job_apply import JobApplyRow

    if getattr(job, "source", None) != "job_applies" or not getattr(job, "jobApplyRowId", None):
        return False
    try:
        row = await JobApplyRow.get(job.jobApplyRowId)
        acc = str(job.accountId)
        where = (row.exportedRows or {}).get(acc) if row else None
        if not where or str(row.tailored.get(acc)) != str(job.id):
            return False  # never exported, or a newer tailored resume replaced this one
        account = await Account.get(PydanticObjectId(acc))
        profile = account.name if account else "Profile"
        link = file_links.file_url("tailored", str(job.id))
        tab = where["tab"]
        async with httpx.AsyncClient() as http:
            values = await google_sheets.read_range(http, where["sheetId"], google_sheets._a1(tab, "A1:E"))
            n = find_row(values, recorded=int(where["row"]), url=row.url, profile=profile)
            if n is None:
                log.info("exported row for %s / %s is gone from %s; link not filled", row.url, profile, tab)
                return False
            await google_sheets.update_range(
                http, where["sheetId"], google_sheets._a1(tab, f"E{n}"), [[f'=HYPERLINK("{link}","Download")']],
            )
        if n != int(where["row"]):
            await JobApplyRow.get_motor_collection().update_one(
                {"_id": row.id}, {"$set": {f"exportedRows.{acc}.row": n}}
            )
        return True
    except Exception as exc:  # noqa: BLE001 — never fail the resume job over the sheet
        log.warning("filling the sheet link for resume %s failed: %s", getattr(job, "id", "?"), exc)
        return False
