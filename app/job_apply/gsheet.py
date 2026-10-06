"""Read a job sheet straight from a Google Sheets link (sheet shared as "Anyone with the link can view").

The linked tab is downloaded as CSV. CSV keeps only a cell's display text, so when the links sit behind
text ("Apply") and no URL column is found, the workbook is downloaded as .xlsx instead, which keeps
hyperlinks; that reads the workbook's first tab.
"""
import logging
import re
from typing import Optional
from urllib.parse import unquote, urlsplit

import httpx

from app.job_apply.sheet import MAX_BYTES, SheetError, SheetRow, parse_sheet, parse_sheet_ex

log = logging.getLogger(__name__)

EXPORT_URL = "https://docs.google.com/spreadsheets/d/{sid}/export"
PUBLISHED_URL = "https://docs.google.com/spreadsheets/d/{sid}/pub"  # sid "e/<published id>" (File > Publish to web)
TIMEOUT = 30.0
DEFAULT_TITLE = "Google Sheet"
PRIVATE_HINT = (
    "This sheet isn't shared by link. In Google Sheets: Share → General access → Anyone with the link (Viewer)."
)

_PATH = re.compile(r"^/spreadsheets/(?:u/\d+/)?d/(e/[A-Za-z0-9_-]{10,}|[A-Za-z0-9_-]{10,})")
_GID = re.compile(r"(?:^|[&#?])gid=(\d+)")
_CD_STAR = re.compile(r"filename\*\s*=\s*(?:UTF-8|utf-8)''([^;]+)")
_CD = re.compile(r'filename\s*=\s*"([^"]+)"')


def parse_gsheet_url(url: str) -> tuple[str, Optional[str]]:
    """Google Sheets link → (spreadsheet id, tab gid or None for the first tab). A published sheet's id is
    "e/<published id>". When the link names the tab twice (?gid=0#gid=5) the last one is the tab on screen."""
    raw = (url or "").strip()
    if raw.lower().startswith("docs.google.com/"):
        raw = "https://" + raw
    parts = urlsplit(raw)
    m = _PATH.match(parts.path) if parts.hostname == "docs.google.com" else None
    if not m:
        raise SheetError("Paste a Google Sheets link (https://docs.google.com/spreadsheets/d/…).")
    gids = _GID.findall(f"?{parts.query}#{parts.fragment}")
    return m.group(1), (gids[-1] if gids else None)


def _title(resp: httpx.Response) -> str:
    cd = resp.headers.get("content-disposition", "")
    m = _CD_STAR.search(cd)
    name = unquote(m.group(1)) if m else (_CD.search(cd).group(1) if _CD.search(cd) else "")
    name = re.sub(r"\.(csv|xlsx)$", "", name.strip(), flags=re.I)
    return name or DEFAULT_TITLE


async def _export(client: httpx.AsyncClient, sid: str, fmt: str, gid: Optional[str]) -> httpx.Response:
    published = sid.startswith("e/")
    params = {("output" if published else "format"): fmt, **({"gid": gid} if gid else {})}
    url = (PUBLISHED_URL if published else EXPORT_URL).format(sid=sid)
    try:
        resp = await client.get(url, params=params, timeout=TIMEOUT, follow_redirects=True)
    except httpx.HTTPError as exc:
        log.warning("google sheet export failed for %s (%s): %r", sid, params, exc)
        raise SheetError("Could not reach Google Sheets. Try again in a moment.") from exc
    if resp.status_code == 404:
        raise SheetError("Google Sheet not found. Check the link.")
    if resp.status_code == 400 and gid:
        # The link names a tab that doesn't exist; the browser opens the first tab then, so do the same.
        return await _export(client, sid, fmt, None)
    if (
        resp.status_code in (401, 403)
        or resp.url.host == "accounts.google.com"
        or "text/html" in resp.headers.get("content-type", "")
    ):
        raise SheetError(PRIVATE_HINT)
    if resp.status_code >= 400:
        raise SheetError(f"Google Sheets returned an error ({resp.status_code}).")
    if len(resp.content) > MAX_BYTES:
        raise SheetError(f"Sheet is larger than {MAX_BYTES // 1_000_000} MB.")
    return resp


async def download_gsheet(url: str, *, client: httpx.AsyncClient) -> tuple[str, list[SheetRow]]:
    """Returns (sheet title, rows). Raises SheetError with a user-facing message."""
    sid, gid = parse_gsheet_url(url)
    csv_resp = await _export(client, sid, "csv", gid)
    title = _title(csv_resp)
    try:
        rows, by_column = parse_sheet_ex("sheet.csv", csv_resp.content)
    except SheetError as csv_error:
        if "URL column" not in str(csv_error):
            raise
        xlsx_resp = await _export(client, sid, "xlsx", None)
        try:
            return title, parse_sheet("sheet.xlsx", xlsx_resp.content)
        except SheetError:
            raise csv_error
    if by_column:
        return title, rows
    # Only stray links in the CSV: the job links may sit behind text ("Apply"), which only the xlsx keeps.
    try:
        xlsx_rows = parse_sheet("sheet.xlsx", (await _export(client, sid, "xlsx", None)).content)
    except SheetError:
        return title, rows
    return title, (xlsx_rows if len(xlsx_rows) > len(rows) else rows)
