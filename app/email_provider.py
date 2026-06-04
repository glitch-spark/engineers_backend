"""Provider dispatch — picks Gmail or Outlook callable for a given
EmailAccount. Keeps the sync orchestrator free of `if provider == "gmail"
elif provider == "outlook"` ladders.

Each call returns the same normalized shape so the orchestrator never
branches downstream. Adding a third provider (yahoo, fastmail, etc) is a
matter of adding a new wrapper module + one entry in `_PROVIDERS`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from app import gmail_api, outlook_api
from app.models import EmailAccount


class ProviderError(RuntimeError):
    """Generic wrapper around upstream provider errors. The orchestrator
    catches this to surface a single 401/502 path regardless of which
    provider raised."""

    def __init__(self, status: int, message: str):
        super().__init__(f"provider error {status}: {message}")
        self.status = status
        self.message = message


def _wrap_errors(fn):
    async def wrapped(*a, **kw):
        try:
            return await fn(*a, **kw)
        except gmail_api.GmailApiError as exc:
            raise ProviderError(exc.status, exc.message) from exc
        except outlook_api.OutlookApiError as exc:
            raise ProviderError(exc.status, exc.message) from exc
    return wrapped


# ---------------------------------------------------------------------------
# OAuth surface
# ---------------------------------------------------------------------------


def build_authorization_url(provider: str, *, state: str, login_hint: Optional[str] = None) -> str:
    if provider == "gmail":
        return gmail_api.build_authorization_url(state=state, login_hint=login_hint)
    if provider == "outlook":
        return outlook_api.build_authorization_url(state=state, login_hint=login_hint)
    raise ProviderError(400, f"unknown provider {provider}")


async def exchange_code(provider: str, code: str) -> dict[str, Any]:
    if provider == "gmail":
        try:
            return await gmail_api.exchange_code(code)
        except gmail_api.GmailApiError as exc:
            raise ProviderError(exc.status, exc.message) from exc
    if provider == "outlook":
        try:
            return await outlook_api.exchange_code(code)
        except outlook_api.OutlookApiError as exc:
            raise ProviderError(exc.status, exc.message) from exc
    raise ProviderError(400, f"unknown provider {provider}")


async def refresh_access_token(provider: str, refresh_token: str) -> str:
    if provider == "gmail":
        try:
            return await gmail_api.refresh_access_token(refresh_token)
        except gmail_api.GmailApiError as exc:
            raise ProviderError(exc.status, exc.message) from exc
    if provider == "outlook":
        try:
            return await outlook_api.refresh_access_token(refresh_token)
        except outlook_api.OutlookApiError as exc:
            raise ProviderError(exc.status, exc.message) from exc
    raise ProviderError(400, f"unknown provider {provider}")


async def fetch_userinfo(provider: str, access_token: str) -> dict[str, Any]:
    if provider == "gmail":
        try:
            data = await gmail_api.fetch_userinfo(access_token)
        except gmail_api.GmailApiError as exc:
            raise ProviderError(exc.status, exc.message) from exc
        return {"email": data.get("email"), "name": data.get("name")}
    if provider == "outlook":
        try:
            data = await outlook_api.fetch_userinfo(access_token)
        except outlook_api.OutlookApiError as exc:
            raise ProviderError(exc.status, exc.message) from exc
        return {
            "email": (data.get("mail") or data.get("userPrincipalName") or "").lower(),
            "name": data.get("displayName"),
        }
    raise ProviderError(400, f"unknown provider {provider}")


async def revoke_refresh_token(provider: str, refresh_token: str) -> None:
    if provider == "gmail":
        await gmail_api.revoke_refresh_token(refresh_token)
    elif provider == "outlook":
        await outlook_api.revoke_refresh_token(refresh_token)


# ---------------------------------------------------------------------------
# Mail list / get
# ---------------------------------------------------------------------------


async def list_initial_message_ids(
    account: EmailAccount,
    *,
    access_token: str,
    days: int,
    max_results: int,
) -> tuple[list[str], str]:
    """Initial date-bounded pull. Returns (message_ids, cursor_for_next_sync).
    For Gmail the cursor is the profile.historyId; for Outlook it is the
    deltaLink seeded by an initial /delta call."""
    ids: list[str] = []
    if account.provider == "gmail":
        try:
            query = f"in:inbox newer_than:{days}d"
            page_token = None
            while len(ids) < max_results:
                page = await gmail_api.list_message_ids(
                    access_token, query=query, page_token=page_token, max_results=100,
                )
                for m in page.get("messages") or []:
                    if m.get("id"):
                        ids.append(m["id"])
                page_token = page.get("nextPageToken")
                if not page_token:
                    break
            profile = await gmail_api.get_profile(access_token)
            return ids, str(profile.get("historyId") or "")
        except gmail_api.GmailApiError as exc:
            raise ProviderError(exc.status, exc.message) from exc

    if account.provider == "outlook":
        try:
            from datetime import timedelta, timezone as tz
            since = datetime.now(tz.utc) - timedelta(days=days)
            page_token = None
            while len(ids) < max_results:
                page = await outlook_api.list_message_ids(
                    access_token, since=since, page_token=page_token, max_results=100,
                )
                for m in page.get("messages") or []:
                    if m.get("id"):
                        ids.append(m["id"])
                page_token = page.get("nextPageToken")
                if not page_token:
                    break
            # Seed a delta cursor for next sync — empty `delta_link` means
            # start fresh. We do a single seed call and grab its deltaLink
            # to avoid replaying the initial pull twice.
            seed = await outlook_api.list_delta(access_token, delta_link=None)
            cursor = seed.get("deltaLink") or ""
            return ids, cursor
        except outlook_api.OutlookApiError as exc:
            raise ProviderError(exc.status, exc.message) from exc

    raise ProviderError(400, f"unknown provider {account.provider}")


async def list_delta_message_ids(
    account: EmailAccount,
    *,
    access_token: str,
    cursor: str,
    max_results: int,
) -> tuple[list[str], str, bool]:
    """Delta pull. Returns (message_ids, new_cursor, cursor_expired).
    cursor_expired=True signals the orchestrator to fall back to a short
    date-bounded re-pull (Gmail history is ~7 days; Outlook delta is
    similar in practice)."""
    ids: list[str] = []
    if account.provider == "gmail":
        try:
            page_token = None
            seen: set[str] = set()
            new_cursor = cursor
            while True:
                page = await gmail_api.list_history(
                    access_token, start_history_id=cursor, page_token=page_token,
                )
                for h in page.get("history") or []:
                    new_cursor = str(h.get("id") or new_cursor)
                    for added in h.get("messagesAdded") or []:
                        mid = (added.get("message") or {}).get("id")
                        if mid and mid not in seen:
                            seen.add(mid)
                            ids.append(mid)
                            if len(ids) >= max_results:
                                break
                    if len(ids) >= max_results:
                        break
                page_token = page.get("nextPageToken")
                if not page_token or len(ids) >= max_results:
                    break
            return ids, new_cursor, False
        except gmail_api.GmailApiError as exc:
            if exc.status == 404:
                return [], cursor, True
            raise ProviderError(exc.status, exc.message) from exc

    if account.provider == "outlook":
        try:
            page = await outlook_api.list_delta(access_token, delta_link=cursor or None)
            for m in page.get("messages") or []:
                if m.get("id"):
                    ids.append(m["id"])
                    if len(ids) >= max_results:
                        break
            new_cursor = page.get("deltaLink") or cursor
            return ids, new_cursor, False
        except outlook_api.OutlookApiError as exc:
            # Outlook returns 410 Gone when the delta cursor is expired.
            if exc.status in (410, 404):
                return [], cursor, True
            raise ProviderError(exc.status, exc.message) from exc

    raise ProviderError(400, f"unknown provider {account.provider}")


async def get_message_metadata(account: EmailAccount, access_token: str, message_id: str) -> dict[str, Any]:
    """Returns the *parsed* metadata dict (already normalized), not raw."""
    if account.provider == "gmail":
        try:
            raw = await gmail_api.get_message_metadata(access_token, message_id)
            return gmail_api.parse_message_metadata(raw)
        except gmail_api.GmailApiError as exc:
            if exc.status == 404:
                return {}
            raise ProviderError(exc.status, exc.message) from exc
    if account.provider == "outlook":
        try:
            raw = await outlook_api.get_message_metadata(access_token, message_id)
            return outlook_api.parse_message_metadata(raw)
        except outlook_api.OutlookApiError as exc:
            if exc.status == 404:
                return {}
            raise ProviderError(exc.status, exc.message) from exc
    raise ProviderError(400, f"unknown provider {account.provider}")


async def fetch_message_body(account: EmailAccount, access_token: str, message_id: str) -> str:
    if account.provider == "gmail":
        try:
            return await gmail_api.fetch_message_body(access_token, message_id)
        except gmail_api.GmailApiError as exc:
            if exc.status == 404:
                return ""
            raise ProviderError(exc.status, exc.message) from exc
    if account.provider == "outlook":
        try:
            return await outlook_api.fetch_message_body(access_token, message_id)
        except outlook_api.OutlookApiError as exc:
            if exc.status == 404:
                return ""
            raise ProviderError(exc.status, exc.message) from exc
    raise ProviderError(400, f"unknown provider {account.provider}")
