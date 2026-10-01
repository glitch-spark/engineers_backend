"""Write Job Applies exports into a Google Sheet through a service account.

The user shares one sheet with the service account's email as Editor; the app adds a tab per day and appends
rows. Auth is the OAuth 2.0 JWT-bearer flow (an RS256-signed assertion exchanged for an access token).
"""
import json
import os
import time
from typing import Any, Optional
from urllib.parse import quote

import httpx
from jose import jwt

from app.config import get_settings

SCOPE = "https://www.googleapis.com/auth/spreadsheets"
API = "https://sheets.googleapis.com/v4/spreadsheets"
TIMEOUT = 30.0
VALIDATION_ROWS = 5000  # rows that get the Applied checkbox when a tab is created

_token_cache: dict[str, tuple[str, float]] = {}


class SheetsError(Exception):
    """A user-facing problem with the export (not set up, not shared, Google error)."""


def _service_account_raw() -> Optional[str]:
    return get_settings().GOOGLE_SERVICE_ACCOUNT_JSON


def service_account() -> Optional[dict]:
    raw = (_service_account_raw() or "").strip()
    if not raw:
        return None
    if not raw.startswith("{") and os.path.isfile(raw):
        with open(raw, encoding="utf-8") as fh:
            raw = fh.read()
    try:
        info = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SheetsError("GOOGLE_SERVICE_ACCOUNT_JSON isn't valid JSON.") from exc
    if not info.get("client_email") or not info.get("private_key"):
        raise SheetsError("GOOGLE_SERVICE_ACCOUNT_JSON is missing client_email or private_key.")
    return info


def service_account_email() -> Optional[str]:
    try:
        info = service_account()
    except SheetsError:
        return None
    return info["client_email"] if info else None


def _require() -> dict:
    info = service_account()
    if not info:
        raise SheetsError(
            "Google Sheets export isn't set up: the server needs GOOGLE_SERVICE_ACCOUNT_JSON (a service account key)."
        )
    return info


async def _token(client: httpx.AsyncClient) -> str:
    info = _require()
    email = info["client_email"]
    cached = _token_cache.get(email)
    if cached and cached[1] > time.time() + 60:
        return cached[0]
    token_uri = info.get("token_uri") or "https://oauth2.googleapis.com/token"
    now = int(time.time())
    assertion = jwt.encode(
        {"iss": email, "scope": SCOPE, "aud": token_uri, "iat": now, "exp": now + 3600},
        info["private_key"],
        algorithm="RS256",
    )
    resp = await client.post(
        token_uri,
        data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion},
        timeout=TIMEOUT,
    )
    if resp.status_code >= 400:
        raise SheetsError("Google rejected the service account key. Check GOOGLE_SERVICE_ACCOUNT_JSON.")
    body = resp.json()
    _token_cache[email] = (body["access_token"], time.time() + int(body.get("expires_in", 3600)))
    return body["access_token"]


async def _call(client: httpx.AsyncClient, method: str, url: str, **kw: Any) -> dict:
    token = await _token(client)
    try:
        resp = await client.request(method, url, headers={"Authorization": f"Bearer {token}"}, timeout=TIMEOUT, **kw)
    except httpx.HTTPError as exc:
        raise SheetsError("Could not reach Google Sheets. Try again in a moment.") from exc
    if resp.status_code in (403, 404):
        raise SheetsError(
            f"The app can't edit that sheet. Share it with {_require()['client_email']} as Editor, then try again."
        )
    if resp.status_code >= 400:
        try:
            message = resp.json().get("error", {}).get("message", "")
        except ValueError:
            message = ""
        raise SheetsError(f"Google Sheets error ({resp.status_code}){': ' + message if message else ''}.")
    return resp.json() if resp.content else {}


def _a1(tab: str, cell: str) -> str:
    return "'" + tab.replace("'", "''") + "'!" + cell


async def sheet_info(client: httpx.AsyncClient, sheet_id: str) -> dict:
    """{'title': spreadsheet title, 'tabs': {tab title: sheetId}}"""
    body = await _call(
        client, "GET", f"{API}/{sheet_id}", params={"fields": "properties.title,sheets.properties(sheetId,title)"}
    )
    tabs = {s["properties"]["title"]: s["properties"]["sheetId"] for s in body.get("sheets", [])}
    return {"title": body.get("properties", {}).get("title", ""), "tabs": tabs}


async def ensure_tab(client: httpx.AsyncClient, sheet_id: str, title: str, header: list[str], info: dict) -> bool:
    """Create the tab with a bold, frozen header and an Applied checkbox column if it doesn't exist."""
    if title in info["tabs"]:
        return False
    added = await _call(
        client, "POST", f"{API}/{sheet_id}:batchUpdate", json={"requests": [{"addSheet": {"properties": {"title": title}}}]}
    )
    tab_id = added["replies"][0]["addSheet"]["properties"]["sheetId"]
    info["tabs"][title] = tab_id
    last = len(header) - 1
    await _call(
        client, "PUT", f"{API}/{sheet_id}/values/{quote(_a1(title, 'A1'), safe='')}",
        params={"valueInputOption": "RAW"}, json={"values": [header]},
    )
    await _call(client, "POST", f"{API}/{sheet_id}:batchUpdate", json={"requests": [
        {"repeatCell": {"range": {"sheetId": tab_id, "startRowIndex": 0, "endRowIndex": 1},
                        "cell": {"userEnteredFormat": {"textFormat": {"bold": True}}},
                        "fields": "userEnteredFormat.textFormat.bold"}},
        {"updateSheetProperties": {"properties": {"sheetId": tab_id, "gridProperties": {"frozenRowCount": 1}},
                                   "fields": "gridProperties.frozenRowCount"}},
        {"setDataValidation": {"range": {"sheetId": tab_id, "startRowIndex": 1, "endRowIndex": VALIDATION_ROWS,
                                         "startColumnIndex": last, "endColumnIndex": last + 1},
                               "rule": {"condition": {"type": "BOOLEAN"}}}},
    ]})
    return True


async def append_rows(client: httpx.AsyncClient, sheet_id: str, tab: str, rows: list[list[Any]]) -> None:
    """Append rows after the tab's data; formulas (=HYPERLINK) are evaluated."""
    if not rows:
        return
    await _call(
        client, "POST", f"{API}/{sheet_id}/values/{quote(_a1(tab, 'A1'), safe='')}:append",
        params={"valueInputOption": "USER_ENTERED", "insertDataOption": "INSERT_ROWS"}, json={"values": rows},
    )
