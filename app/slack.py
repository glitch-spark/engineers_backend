import logging
from datetime import datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

import httpx

from app.config import get_settings

log = logging.getLogger(__name__)


def _fmt_usd(n: float) -> str:
    return f"${n:,.2f}"


def _app_base_url() -> str:
    return get_settings().APP_BASE_URL.rstrip("/")


def _now_str() -> str:
    tz = ZoneInfo(get_settings().APP_TIMEZONE)
    return datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")


async def _post_to_slack(payload: dict[str, Any]) -> None:
    url = get_settings().SLACK_WEBHOOK_URL
    if not url:
        return
    async with httpx.AsyncClient(timeout=10.0) as client:
        await client.post(url, json=payload)


async def notify_new_transaction(
    *,
    creator_email: str,
    amount: float,
    date: str,
    description: Optional[str] = None,
) -> None:
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
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Open Transactions"},
                        "url": f"{_app_base_url()}/transactions",
                    }
                ],
            },
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"Posted by your app • {_now_str()}"}],
            },
        ]
        await _post_to_slack({"text": text, "blocks": blocks})
    except Exception as e:
        log.warning("slack notify_new_transaction failed: %s", e)


async def notify_new_interview(
    *,
    creator_email: str,
    account_email: str,
    stage: str,
    scheduled_at: str,
) -> None:
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
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Open Interviews"},
                        "url": f"{_app_base_url()}/interviews",
                    }
                ],
            },
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"Posted by your app • {_now_str()}"}],
            },
        ]
        await _post_to_slack({"text": text, "blocks": blocks})
    except Exception as e:
        log.warning("slack notify_new_interview failed: %s", e)


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
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Open Transactions"},
                        "url": f"{_app_base_url()}/transactions",
                    }
                ],
            }
        )
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"Posted by your app • {_now_str()}"}],
            }
        )
        await _post_to_slack({"text": text, "blocks": blocks})
    except Exception as e:
        log.warning("slack notify_transaction_status_change failed: %s", e)
