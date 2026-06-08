"""Thin async wrapper over MS Graph + Microsoft Identity OAuth.

Mirrors the surface of `gmail_api` so the sync orchestrator can dispatch
on `EmailAccount.provider` without branching on call shape. Where Gmail
returns flat metadata, Graph returns nested $select projections; we
normalize to the same `ParsedMessage` shape inside `parse_message_metadata`.

Outlook's delta cursor (`@odata.deltaLink`) is a full URL, not an opaque
ID like Gmail's historyId. We store the full deltaLink in
`EmailAccount.historyId` and detect outlook-shaped cursors by the
`https://` prefix.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from email.utils import parseaddr
from typing import Any, Optional
from urllib.parse import urlencode

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)


# Microsoft Identity endpoints. `{tenant}` is replaced at call time.
_MS_AUTHZ_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/authorize"
_MS_TOKEN_URL = "https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"

# Scopes we request. offline_access is REQUIRED for refresh tokens.
# Mail.Read is the read-only mailbox scope (no body modify / no send).
OUTLOOK_SCOPES = [
    "openid",
    "profile",
    "email",
    "offline_access",
    "User.Read",
    "Mail.Read",
]


class OutlookApiError(RuntimeError):
    """Mirrors GmailApiError so the orchestrator can catch a single
    pattern: `except (GmailApiError, OutlookApiError):`."""

    def __init__(self, status: int, message: str):
        super().__init__(f"outlook api {status}: {message}")
        self.status = status
        self.message = message


def _tenant() -> str:
    return get_settings().MS_OAUTH_TENANT or "common"


def _authz_endpoint() -> str:
    return _MS_AUTHZ_URL.format(tenant=_tenant())


def _token_endpoint() -> str:
    return _MS_TOKEN_URL.format(tenant=_tenant())


# ---------------------------------------------------------------------------
# OAuth
# ---------------------------------------------------------------------------


def build_authorization_url(*, state: str, login_hint: Optional[str] = None) -> str:
    s = get_settings()
    if not s.MS_OAUTH_CLIENT_ID:
        raise OutlookApiError(503, "MS_OAUTH_CLIENT_ID is not configured")
    params = {
        "client_id": s.MS_OAUTH_CLIENT_ID,
        "redirect_uri": s.MS_OAUTH_REDIRECT_URI,
        "response_type": "code",
        "response_mode": "query",
        "scope": " ".join(OUTLOOK_SCOPES),
        "state": state,
        # select_account shows Microsoft's account picker so the user can
        # connect ANY Outlook account instead of being pinned to one. Refresh
        # tokens still come back via the offline_access scope above
        # (prompt=consent is not required to receive one).
        "prompt": "select_account",
    }
    if login_hint:
        params["login_hint"] = login_hint
    return f"{_authz_endpoint()}?{urlencode(params)}"


async def exchange_code(code: str) -> dict[str, Any]:
    s = get_settings()
    if not (s.MS_OAUTH_CLIENT_ID and s.MS_OAUTH_CLIENT_SECRET):
        raise OutlookApiError(503, "Microsoft OAuth credentials are not configured")
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.post(
            _token_endpoint(),
            data={
                "client_id": s.MS_OAUTH_CLIENT_ID,
                "client_secret": s.MS_OAUTH_CLIENT_SECRET,
                "code": code,
                "redirect_uri": s.MS_OAUTH_REDIRECT_URI,
                "grant_type": "authorization_code",
                "scope": " ".join(OUTLOOK_SCOPES),
            },
        )
    if r.status_code >= 400:
        raise OutlookApiError(r.status_code, r.text)
    return r.json()


async def refresh_access_token(refresh_token: str) -> str:
    s = get_settings()
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.post(
            _token_endpoint(),
            data={
                "client_id": s.MS_OAUTH_CLIENT_ID,
                "client_secret": s.MS_OAUTH_CLIENT_SECRET,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
                "scope": " ".join(OUTLOOK_SCOPES),
            },
        )
    if r.status_code >= 400:
        raise OutlookApiError(r.status_code, r.text)
    return r.json()["access_token"]


async def fetch_userinfo(access_token: str) -> dict[str, Any]:
    """Graph `/me` returns userPrincipalName + mail. We prefer `mail` —
    `userPrincipalName` is sometimes the AAD upn instead of the real
    SMTP address."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(
            f"{GRAPH_BASE}/me",
            headers={"Authorization": f"Bearer {access_token}"},
        )
    if r.status_code >= 400:
        raise OutlookApiError(r.status_code, r.text)
    return r.json()


async def revoke_refresh_token(refresh_token: str) -> None:
    """Microsoft Identity has no direct token-revoke endpoint analogous
    to Google's. Soft-disconnect by wiping the stored credential is
    sufficient; if the user revokes from their MSA security panel that
    invalidates our token upstream anyway. This is a no-op kept for API
    parity with `gmail_api.revoke_refresh_token`."""
    return None


# ---------------------------------------------------------------------------
# Mail list / get
# ---------------------------------------------------------------------------


async def list_message_ids(
    access_token: str,
    *,
    query: Optional[str] = None,
    page_token: Optional[str] = None,
    max_results: int = 100,
    since: Optional[datetime] = None,
) -> dict[str, Any]:
    """Initial pull. Graph uses $filter/$top/$skip pagination — we map
    the Gmail-style `query="newer_than:Nd"` to a $filter on
    receivedDateTime. The orchestrator passes `since` when it can; we
    fall back to parsing `newer_than:Nd` for callers wired the Gmail way.

    Returns {messages: [{id}], nextPageToken?: str}."""
    if page_token:
        # Graph @odata.nextLink is a full URL — fetch it directly.
        async with httpx.AsyncClient(timeout=20.0) as client:
            r = await client.get(page_token, headers={"Authorization": f"Bearer {access_token}"})
        if r.status_code >= 400:
            raise OutlookApiError(r.status_code, r.text)
        data = r.json()
        return {
            "messages": [{"id": m["id"]} for m in data.get("value") or []],
            "nextPageToken": data.get("@odata.nextLink"),
        }

    params: dict[str, Any] = {
        "$top": str(max_results),
        "$select": "id,internetMessageId,subject,bodyPreview,from,receivedDateTime,conversationId,categories",
        "$orderby": "receivedDateTime DESC",
    }
    if since is None and query:
        m = re.search(r"newer_than:(\d+)d", query)
        if m:
            days = int(m.group(1))
            since = datetime.now(timezone.utc).replace(microsecond=0)
            since = since.replace(hour=0, minute=0, second=0)
            from datetime import timedelta
            since = since - timedelta(days=days)
    if since is not None:
        # ISO-8601 with Z suffix as Graph expects.
        iso = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        params["$filter"] = f"receivedDateTime ge {iso}"

    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.get(
            f"{GRAPH_BASE}/me/mailFolders/inbox/messages",
            params=params,
            headers={"Authorization": f"Bearer {access_token}"},
        )
    if r.status_code >= 400:
        raise OutlookApiError(r.status_code, r.text)
    data = r.json()
    return {
        "messages": [{"id": m["id"]} for m in data.get("value") or []],
        "nextPageToken": data.get("@odata.nextLink"),
    }


async def list_delta(
    access_token: str,
    *,
    delta_link: Optional[str] = None,
) -> dict[str, Any]:
    """Wraps the inbox delta API. First call (no delta_link) returns a
    fresh seed delta; subsequent calls use the prior @odata.deltaLink to
    fetch only changes. Returns {messages: [{id}], deltaLink: str|None}.

    Graph paginates with `@odata.nextLink`; the LAST page carries
    `@odata.deltaLink` which is the cursor for the next sync."""
    url = delta_link or f"{GRAPH_BASE}/me/mailFolders/inbox/messages/delta?$select=id,internetMessageId,subject,bodyPreview,from,receivedDateTime,conversationId,categories"
    new_messages: list[dict[str, Any]] = []
    new_delta_link: Optional[str] = None
    async with httpx.AsyncClient(timeout=20.0) as client:
        while url:
            r = await client.get(url, headers={"Authorization": f"Bearer {access_token}", "Prefer": "odata.maxpagesize=100"})
            if r.status_code >= 400:
                raise OutlookApiError(r.status_code, r.text)
            data = r.json()
            for m in data.get("value") or []:
                # Tombstones for deleted messages have `@removed`; skip them.
                if "@removed" in m:
                    continue
                if m.get("id"):
                    new_messages.append({"id": m["id"]})
            url = data.get("@odata.nextLink")
            if data.get("@odata.deltaLink"):
                new_delta_link = data["@odata.deltaLink"]
    return {"messages": new_messages, "deltaLink": new_delta_link}


async def get_message_metadata(access_token: str, message_id: str) -> dict[str, Any]:
    """Returns the raw Graph message object. We project the same fields
    list_message_ids/list_delta select, plus webLink so the drawer can
    deep-link out."""
    params = {
        "$select": "id,internetMessageId,subject,bodyPreview,from,receivedDateTime,conversationId,categories,webLink",
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.get(
            f"{GRAPH_BASE}/me/messages/{message_id}",
            params=params,
            headers={"Authorization": f"Bearer {access_token}"},
        )
    if r.status_code >= 400:
        raise OutlookApiError(r.status_code, r.text)
    return r.json()


def parse_message_metadata(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize Graph payload to the same dict shape Gmail returns."""
    from_obj = (raw.get("from") or {}).get("emailAddress") or {}
    from_addr = (from_obj.get("address") or "").lower()
    from_name = from_obj.get("name")
    subject = raw.get("subject") or ""
    snippet = raw.get("bodyPreview") or ""
    received = raw.get("receivedDateTime")
    if received:
        try:
            received_at = datetime.fromisoformat(received.replace("Z", "+00:00"))
        except ValueError:
            received_at = datetime.now(timezone.utc)
    else:
        received_at = datetime.now(timezone.utc)
    categories = raw.get("categories") or []
    return {
        "messageId": raw.get("id"),
        "threadId": raw.get("conversationId") or raw.get("id"),
        "historyId": None,  # outlook has no per-message history id
        "fromAddress": from_addr,
        "fromName": from_name,
        "subject": subject[:500],
        "snippet": snippet[:1000],
        "receivedAt": received_at,
        # Outlook uses user-defined categories; treat as labels for parity.
        "labelIds": categories,
        # Promotions-equivalent signal: bodyPreview-style categories + unsub headers
        "isUnsub": False,  # not exposed in metadata projection
    }


async def fetch_message_body(access_token: str, message_id: str) -> str:
    """Returns plain-text body. Graph returns body as {contentType, content};
    we prefer text/plain, fall back to stripped HTML."""
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.get(
            f"{GRAPH_BASE}/me/messages/{message_id}",
            params={"$select": "body,bodyPreview"},
            headers={"Authorization": f"Bearer {access_token}", "Prefer": 'outlook.body-content-type="text"'},
        )
    if r.status_code >= 400:
        raise OutlookApiError(r.status_code, r.text)
    data = r.json()
    body = (data.get("body") or {}).get("content") or data.get("bodyPreview") or ""
    if (data.get("body") or {}).get("contentType") == "html":
        from html import unescape
        body = unescape(re.sub(r"<[^>]+>", " ", body))
        body = re.sub(r"\s+", " ", body).strip()
    return body
