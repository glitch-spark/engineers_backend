"""Thin async wrapper over the Gmail REST API + Google OAuth token flow.

We deliberately avoid `google-api-python-client` — it pulls a sync HTTP
stack and a multi-MB dependency tree just for two endpoints we hit.
`httpx` is already a dependency for the rest of the backend.

What this module owns:
  - exchanging an OAuth authorization code for tokens
  - refreshing an access token from a stored refresh token
  - listing message IDs in a date window or via history delta
  - fetching message metadata (headers + snippet) by ID
  - revoking a refresh token on disconnect

What it does NOT own:
  - persistence (callers handle EmailAccount / EmailMessage writes)
  - classification (see email_classifier)
  - any state — every call is stateless against the upstream API
"""

from __future__ import annotations

import base64
import logging
from datetime import datetime, timezone
from email.utils import parseaddr
from typing import Any, Optional
from urllib.parse import urlencode

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)


# Google constants — endpoints, not user-tunable.
GOOGLE_OAUTH_AUTHZ_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_OAUTH_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_OAUTH_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v2/userinfo"
GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1/users/me"

# Scopes we request. Keep in sync with the OAuth consent screen config in
# Google Cloud — Google rejects token exchange if the requested scope set
# isn't a subset of the configured scopes.
GMAIL_SCOPES = [
    "openid",
    "email",
    "profile",
    "https://www.googleapis.com/auth/gmail.readonly",
]


class GmailApiError(RuntimeError):
    """Wraps any non-2xx upstream response. Lets callers distinguish "user
    revoked us" (401) from "bad request" (4xx) from "transient" (5xx)."""

    def __init__(self, status: int, message: str):
        super().__init__(f"gmail api {status}: {message}")
        self.status = status
        self.message = message


# ---------------------------------------------------------------------------
# OAuth
# ---------------------------------------------------------------------------


def build_authorization_url(*, state: str, login_hint: Optional[str] = None) -> str:
    """Returns the URL the SPA should redirect/popup to. State is opaque to
    Google — we use it to bind the callback to the originating user."""
    s = get_settings()
    if not s.GOOGLE_OAUTH_CLIENT_ID:
        raise GmailApiError(503, "GOOGLE_OAUTH_CLIENT_ID is not configured")
    params = {
        "client_id": s.GOOGLE_OAUTH_CLIENT_ID,
        "redirect_uri": s.GOOGLE_OAUTH_REDIRECT_URI,
        "response_type": "code",
        "scope": " ".join(GMAIL_SCOPES),
        # offline + consent: forces refresh_token issuance even after the
        # first connect. Without these Google only returns a refresh token
        # on the very first authorization per user/app, so re-connects fail.
        "access_type": "offline",
        # select_account shows Google's account chooser so the user can
        # connect ANY Google account, not just the one they're signed into.
        # consent is kept so a refresh_token is always issued (see above).
        "prompt": "select_account consent",
        "include_granted_scopes": "true",
        "state": state,
    }
    if login_hint:
        params["login_hint"] = login_hint
    return f"{GOOGLE_OAUTH_AUTHZ_URL}?{urlencode(params)}"


async def exchange_code(code: str) -> dict[str, Any]:
    """Trades an auth code for {access_token, refresh_token, expires_in, ...}."""
    s = get_settings()
    if not (s.GOOGLE_OAUTH_CLIENT_ID and s.GOOGLE_OAUTH_CLIENT_SECRET):
        raise GmailApiError(503, "Google OAuth credentials are not configured")
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.post(
            GOOGLE_OAUTH_TOKEN_URL,
            data={
                "code": code,
                "client_id": s.GOOGLE_OAUTH_CLIENT_ID,
                "client_secret": s.GOOGLE_OAUTH_CLIENT_SECRET,
                "redirect_uri": s.GOOGLE_OAUTH_REDIRECT_URI,
                "grant_type": "authorization_code",
            },
        )
    if r.status_code >= 400:
        raise GmailApiError(r.status_code, r.text)
    return r.json()


async def refresh_access_token(refresh_token: str) -> str:
    """Exchanges a stored refresh token for a fresh access token."""
    s = get_settings()
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.post(
            GOOGLE_OAUTH_TOKEN_URL,
            data={
                "refresh_token": refresh_token,
                "client_id": s.GOOGLE_OAUTH_CLIENT_ID,
                "client_secret": s.GOOGLE_OAUTH_CLIENT_SECRET,
                "grant_type": "refresh_token",
            },
        )
    if r.status_code >= 400:
        # 400 invalid_grant = user revoked / refresh token expired.
        raise GmailApiError(r.status_code, r.text)
    return r.json()["access_token"]


async def fetch_userinfo(access_token: str) -> dict[str, Any]:
    """We need the email address attached to the access token. Google sends
    it back via the openid /userinfo endpoint."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(
            GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
    if r.status_code >= 400:
        raise GmailApiError(r.status_code, r.text)
    return r.json()


async def revoke_refresh_token(refresh_token: str) -> None:
    """Best-effort revoke on disconnect. Swallow errors — if Google already
    invalidated the token (revocation race), the disconnect still succeeded
    from the user's perspective."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            await client.post(GOOGLE_OAUTH_REVOKE_URL, data={"token": refresh_token})
        except Exception as exc:  # noqa: BLE001
            log.warning("revoke failed (non-fatal): %s", exc)


# ---------------------------------------------------------------------------
# Gmail list / get
# ---------------------------------------------------------------------------


async def list_message_ids(
    access_token: str,
    *,
    query: str,
    page_token: Optional[str] = None,
    max_results: int = 100,
) -> dict[str, Any]:
    """Wraps users.messages.list. Returns {messages: [{id, threadId}], nextPageToken?, resultSizeEstimate}."""
    params = {"q": query, "maxResults": max_results}
    if page_token:
        params["pageToken"] = page_token
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.get(
            f"{GMAIL_API_BASE}/messages",
            params=params,
            headers={"Authorization": f"Bearer {access_token}"},
        )
    if r.status_code >= 400:
        raise GmailApiError(r.status_code, r.text)
    return r.json()


async def get_message_metadata(access_token: str, message_id: str) -> dict[str, Any]:
    """Fetches a message in `metadata` format — headers + snippet + threadId,
    no body. format=metadata cuts payload by ~10x vs full and is enough for
    classification. Body is fetched on demand by a separate call when the
    drawer opens."""
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.get(
            f"{GMAIL_API_BASE}/messages/{message_id}",
            params={
                "format": "metadata",
                "metadataHeaders": ["From", "Subject", "Date", "List-Unsubscribe"],
            },
            headers={"Authorization": f"Bearer {access_token}"},
        )
    if r.status_code >= 400:
        raise GmailApiError(r.status_code, r.text)
    return r.json()


async def get_profile(access_token: str) -> dict[str, Any]:
    """Returns {emailAddress, messagesTotal, threadsTotal, historyId}. The
    historyId here is the starting cursor for incremental sync."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(
            f"{GMAIL_API_BASE}/profile",
            headers={"Authorization": f"Bearer {access_token}"},
        )
    if r.status_code >= 400:
        raise GmailApiError(r.status_code, r.text)
    return r.json()


async def list_history(
    access_token: str,
    *,
    start_history_id: str,
    page_token: Optional[str] = None,
) -> dict[str, Any]:
    """Wraps users.history.list — returns events since start_history_id.
    Google retains history for ~7 days; older cursors return 404 and the
    caller should fall back to a full re-list."""
    params = {
        "startHistoryId": start_history_id,
        "historyTypes": ["messageAdded"],
    }
    if page_token:
        params["pageToken"] = page_token
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.get(
            f"{GMAIL_API_BASE}/history",
            params=params,
            headers={"Authorization": f"Bearer {access_token}"},
        )
    if r.status_code >= 400:
        raise GmailApiError(r.status_code, r.text)
    return r.json()


# ---------------------------------------------------------------------------
# Helpers — header parsing, body fetch (on demand)
# ---------------------------------------------------------------------------


def parse_message_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    """Extracts the fields we persist on EmailMessage from a raw Gmail
    metadata response. Tolerant of missing headers — gmail occasionally
    drops fields on truncated messages."""
    headers = {h["name"].lower(): h["value"] for h in (payload.get("payload") or {}).get("headers") or []}
    from_raw = headers.get("from", "")
    from_name, from_addr = parseaddr(from_raw)
    subject = headers.get("subject", "")
    snippet = payload.get("snippet") or ""
    # internalDate is ms since epoch as a string.
    received_ms = int(payload.get("internalDate") or 0)
    received_at = datetime.fromtimestamp(received_ms / 1000.0, tz=timezone.utc) if received_ms else datetime.now(timezone.utc)
    return {
        "messageId": payload.get("id"),
        "threadId": payload.get("threadId"),
        "historyId": payload.get("historyId"),
        "fromAddress": (from_addr or from_raw).lower(),
        "fromName": from_name or None,
        "subject": subject[:500],
        "snippet": snippet[:1000],
        "receivedAt": received_at,
        "isUnsub": bool(headers.get("list-unsubscribe")),
    }


async def fetch_message_body(access_token: str, message_id: str) -> str:
    """Returns plain-text body. format=full + walk parts. Used only when the
    drawer opens — body is never persisted server-side."""
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.get(
            f"{GMAIL_API_BASE}/messages/{message_id}",
            params={"format": "full"},
            headers={"Authorization": f"Bearer {access_token}"},
        )
    if r.status_code >= 400:
        raise GmailApiError(r.status_code, r.text)
    payload = r.json().get("payload") or {}
    return _walk_for_text(payload)


def _walk_for_text(part: dict[str, Any]) -> str:
    """Depth-first hunt for text/plain; falls back to text/html stripped."""
    mime = part.get("mimeType", "")
    body = part.get("body") or {}
    data = body.get("data")
    if mime == "text/plain" and data:
        return _decode_b64url(data)
    parts = part.get("parts") or []
    # Prefer text/plain anywhere in the subtree.
    for p in parts:
        if p.get("mimeType") == "text/plain":
            d = (p.get("body") or {}).get("data")
            if d:
                return _decode_b64url(d)
    for p in parts:
        sub = _walk_for_text(p)
        if sub:
            return sub
    # Fall back to text/html on the root.
    if mime == "text/html" and data:
        from html import unescape
        import re
        html = _decode_b64url(data)
        text = re.sub(r"<[^>]+>", " ", html)
        return unescape(re.sub(r"\s+", " ", text)).strip()
    return ""


def _decode_b64url(data: str) -> str:
    try:
        return base64.urlsafe_b64decode(data.encode()).decode("utf-8", errors="replace")
    except Exception:
        return ""
