import logging
from datetime import datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)


def _fmt_usd(n: float) -> str:
    return f"${n:,.2f}"


def _frontend_link(path: str) -> str:
    """User-facing SPA links for Slack messages (not the API base URL)."""
    base = get_settings().FRONTEND_ORIGIN.rstrip("/")
    return f"{base}{path}"


def _now_str() -> str:
    tz_name = (get_settings().APP_TIMEZONE or "").strip() or "UTC"
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        log.warning("slack: invalid APP_TIMEZONE=%r — falling back to UTC", tz_name)
        tz = ZoneInfo("UTC")
    return datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")


def _link_block(label: str, path: str) -> dict[str, Any]:
    url = _frontend_link(path)
    return {"type": "section", "text": {"type": "mrkdwn", "text": f"<{url}|{label}>"}}


async def _post_to_slack(payload: dict[str, Any]) -> bool:
    """Post to Slack incoming webhook. Returns True on success, False otherwise."""
    url = get_settings().SLACK_WEBHOOK_URL
    if not url:
        log.debug("slack: SLACK_WEBHOOK_URL not set, skipping notification")
        return False
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(url, json=payload)
    except httpx.HTTPError as e:
        log.warning("slack webhook request failed: %s", e)
        return False

    body = (resp.text or "").strip()
    if resp.status_code == 200 and body == "ok":
        return True

    log.warning(
        "slack webhook rejected payload: status=%s body=%s",
        resp.status_code,
        body[:500],
    )
    return False


async def notify_new_transaction(
    *,
    creator_email: str,
    amount: float,
    date: str,
    description: Optional[str] = None,
) -> None:
    """Best-effort Slack ping. Must never raise — callers already persisted the row."""
    try:
        text = f"New transaction by {creator_email} ({_fmt_usd(amount)} on {date})"
        blocks: list[dict[str, Any]] = [
            {"type": "header", "text": {"type": "plain_text", "text": "💸 New Transaction", "emoji": True}},
            {"type": "divider"},
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*By:*\n{creator_email or '—'}"},
                    {"type": "mrkdwn", "text": f"*Amount:*\n{_fmt_usd(amount)}"},
                    {"type": "mrkdwn", "text": f"*Date:*\n{date or '—'}"},
                    {"type": "mrkdwn", "text": f"*Description:*\n{description or '—'}"},
                ],
            },
            _link_block("Open Transactions", "/transactions"),
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"Posted by your app • {_now_str()}"}],
            },
        ]
        if not await _post_to_slack({"text": text, "blocks": blocks}):
            log.warning("slack notify_new_transaction failed for %s", creator_email)
    except Exception:
        log.exception("slack notify_new_transaction crashed for %s", creator_email)


async def notify_new_interview(
    *,
    creator_email: str,
    account_email: str,
    stage: str,
    scheduled_at: str,
) -> None:
    """Best-effort Slack ping. Must never raise — callers already persisted the row."""
    try:
        text = f"New interview scheduled by {creator_email} ({stage} on {scheduled_at})"
        blocks: list[dict[str, Any]] = [
            {"type": "header", "text": {"type": "plain_text", "text": "📅 New Interview", "emoji": True}},
            {"type": "divider"},
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*By:*\n{creator_email or '—'}"},
                    {"type": "mrkdwn", "text": f"*Profile:*\n{account_email or '—'}"},
                    {"type": "mrkdwn", "text": f"*Stage:*\n{stage or '—'}"},
                    {"type": "mrkdwn", "text": f"*Scheduled:*\n{scheduled_at or '—'}"},
                ],
            },
            _link_block("Open Interviews", "/interviews"),
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"Posted by your app • {_now_str()}"}],
            },
        ]
        if not await _post_to_slack({"text": text, "blocks": blocks}):
            log.warning("slack notify_new_interview failed for %s", creator_email)
    except Exception:
        log.exception("slack notify_new_interview crashed for %s", creator_email)


async def notify_transaction_status_change(
    *,
    tx_id: str,
    new_status: str,
    approver_email: str,
    owner_email: Optional[str] = None,
    amount: Optional[float] = None,
    date: Optional[str] = None,
    notes: Optional[str] = None,
) -> None:
    """Best-effort Slack ping. Must never raise — callers already persisted the row."""
    try:
        emoji = "✅" if new_status == "approved" else "⛔"
        text = f"Transaction {new_status.upper()} by {approver_email}"
        blocks: list[dict[str, Any]] = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"{emoji} Transaction {new_status.upper()}", "emoji": True},
            },
            {"type": "divider"},
            {
                "type": "section",
                "fields": [
                    {"type": "mrkdwn", "text": f"*Tx ID:*\n{tx_id}"},
                    {"type": "mrkdwn", "text": f"*Owner:*\n{owner_email or '—'}"},
                    {"type": "mrkdwn", "text": f"*Amount:*\n{_fmt_usd(amount) if amount is not None else '—'}"},
                    {"type": "mrkdwn", "text": f"*Date:*\n{date or '—'}"},
                    {"type": "mrkdwn", "text": f"*Approver:*\n{approver_email or '—'}"},
                    {"type": "mrkdwn", "text": f"*Status:*\n{new_status}"},
                ],
            },
        ]
        if notes:
            blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": f"*Notes*\n{notes}"}})
        blocks.append(_link_block("Open Transactions", "/transactions"))
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"Posted by your app • {_now_str()}"}],
            }
        )
        if not await _post_to_slack({"text": text, "blocks": blocks}):
            log.warning("slack notify_transaction_status_change failed for tx %s", tx_id)
    except Exception:
        log.exception("slack notify_transaction_status_change crashed for tx %s", tx_id)
