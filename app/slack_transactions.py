"""Transactions in Slack: the bot posts each one to SLACK_TRANSACTIONS_CHANNEL_ID with Approve / Reject buttons,
edits that post as the row changes and replies in its thread when it is decided.

Every Slack call is best-effort: failures are logged and never fail the request that triggered them.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
from datetime import datetime
from typing import Any, Literal, Optional

import httpx
from beanie import PydanticObjectId

from app.config import get_settings
from app.models import Transaction, UserExtra
from app.slack_bot import _MESSAGE_GONE, _mention, _slack_api, _slack_escape, slack_bot_configured

log = logging.getLogger(__name__)

# Slack rejects a section whose text is longer than this (and with it the whole post).
_SECTION_MAX = 3000

_DECISIONS = {
    "approved": (":white_check_mark:", "Approved"),
    "rejected": (":no_entry:", "Rejected"),
}
_BILLING = {"monthly": "Monthly", "one_time": "One-time"}


def fmt_usd(n: float) -> str:
    return f"${n:,.2f}"


def _method(tx: Any) -> str:
    pay = getattr(tx, "payMethod", None)
    if pay in ("coin", "crypto"):  # 'crypto' is the legacy spelling
        return "Coin"
    if pay != "card":
        return ""
    last4 = (getattr(tx, "cardLast4", None) or "").strip()
    label = (getattr(tx, "cardLabel", None) or "").strip()
    text = f"Card ••{last4}" if last4 else "Card"
    return f"{text} ({_slack_escape(label)})" if label else text


def _clean(value: Optional[str]) -> str:
    return _slack_escape((value or "").strip())


def transaction_text(tx: Any, owner: Optional[UserExtra], payer: Optional[UserExtra]) -> str:
    """The post: owner and amount first, then the transaction's details. Lines without a value are left out."""
    date = getattr(tx, "date", None)
    details = (
        ("Payer", _mention(payer if payer is not None else owner)),
        ("Date", f"{date.strftime('%b')} {date.day}" if date else ""),
        ("Method", _method(tx)),
        ("Billing", _BILLING.get(getattr(tx, "billingCycle", None) or "", "")),
        ("Description", _clean(getattr(tx, "description", None))),
        ("Notes", _clean(getattr(tx, "notes", None))),
    )
    lines = [f":money_with_wings: {_mention(owner)} — *New transaction* · {fmt_usd(tx.amount)}"]
    lines += [f"*{label}:* {value}" for label, value in details if value]
    return "\n".join(lines)


def _section(text: str) -> dict:
    if len(text) > _SECTION_MAX:
        text = text[: _SECTION_MAX - 1] + "…"
    return {"type": "section", "text": {"type": "mrkdwn", "text": text}}


def _button(label: str, action_id: str, style: str, tx: Any) -> dict:
    return {
        "type": "button", "text": {"type": "plain_text", "text": label},
        "action_id": action_id, "value": str(tx.id), "style": style,
    }


def transaction_blocks(
    tx: Any, owner: Optional[UserExtra], payer: Optional[UserExtra], decided_by: Optional[UserExtra]
) -> list[dict]:
    """The post as blocks: its text, then Approve / Reject while pending (only when button clicks can be
    verified, i.e. SLACK_SIGNING_SECRET is set), else who decided it."""
    blocks = [_section(transaction_text(tx, owner, payer))]
    if tx.status == "pending":
        if (get_settings().SLACK_SIGNING_SECRET or "").strip():
            blocks.append({"type": "actions", "elements": [
                _button("Approve", "tx_approve", "primary", tx),
                _button("Reject", "tx_reject", "danger", tx),
            ]})
    elif tx.status in _DECISIONS:
        emoji, word = _DECISIONS[tx.status]
        by = f" by {_mention(decided_by)}" if decided_by is not None else ""
        blocks.append(_section(f"{emoji} *{word}*{by}"))
    return blocks


def _target_channel() -> str:
    return (get_settings().SLACK_TRANSACTIONS_CHANNEL_ID or "").strip()


async def _people(tx: Any) -> tuple[Optional[UserExtra], Optional[UserExtra]]:
    """The transaction's owner and payer (the owner when no payer is stored)."""
    owner = await UserExtra.get(tx.userId)
    if tx.payerId is None or tx.payerId == tx.userId:
        return owner, owner
    return owner, await UserExtra.get(tx.payerId)


async def _edit_post(
    tx: Any, owner: Optional[UserExtra], payer: Optional[UserExtra], decided_by: Optional[UserExtra]
) -> bool:
    """chat.update the stored post. False when there is none, or Slack no longer has it."""
    channel = (tx.slackChannel or "").strip()
    ts = (tx.slackTs or "").strip()
    if not channel or not ts:
        return False
    try:
        await _slack_api("chat.update", {
            "channel": channel, "ts": ts,
            "text": transaction_text(tx, owner, payer), "blocks": transaction_blocks(tx, owner, payer, decided_by),
        })
        return True
    except RuntimeError as exc:
        if str(exc) not in _MESSAGE_GONE:
            raise
        log.info("slack_transactions post %s gone (%s)", ts, exc)
        return False


async def sync_transaction_post(tx: Transaction) -> None:
    """Post the transaction to SLACK_TRANSACTIONS_CHANNEL_ID, or edit its post in place (reposting when the post
    is gone), and remember where it lives. Never raises."""
    try:
        if not slack_bot_configured():
            return
        owner, payer = await _people(tx)
        decided_by = await UserExtra.get(tx.approvedBy) if tx.status != "pending" and tx.approvedBy else None
        if await _edit_post(tx, owner, payer, decided_by):
            return
        channel = _target_channel()
        if not channel:
            return
        res = await _slack_api("chat.postMessage", {
            "channel": channel,
            "text": transaction_text(tx, owner, payer),
            "blocks": transaction_blocks(tx, owner, payer, decided_by),
            "unfurl_links": False, "unfurl_media": False,
        })
        ts = res.get("ts")
        if not ts:
            return
        tx.slackChannel, tx.slackTs = res.get("channel") or channel, ts
        await tx.save()
    except Exception as exc:
        log.warning("slack_transactions sync failed for transaction %s: %s", getattr(tx, "id", "?"), exc)


async def announce_decision(tx: Transaction, by: UserExtra) -> None:
    """Show the decision on the transaction's post (buttons replaced) and reply in its thread; a transaction
    without a post gets the reply as a standalone channel message. Never raises."""
    try:
        if not slack_bot_configured() or tx.status not in _DECISIONS:
            return
        emoji, word = _DECISIONS[tx.status]
        owner, payer = await _people(tx)
        message: dict[str, Any] = {
            "text": f"{emoji} {_mention(owner)} — *{word}* by {_mention(by)} · {fmt_usd(tx.amount)}",
            "unfurl_links": False, "unfurl_media": False,
        }
        if await _edit_post(tx, owner, payer, by):
            message["channel"], message["thread_ts"] = tx.slackChannel, tx.slackTs
        else:
            channel = _target_channel()
            if not channel:
                return
            message["channel"] = channel
        await _slack_api("chat.postMessage", message)
    except Exception as exc:
        log.warning("slack_transactions announce failed for transaction %s: %s", getattr(tx, "id", "?"), exc)


async def delete_transaction_post(tx: Transaction) -> None:
    """Remove the transaction's Slack post, if any. Never raises."""
    channel = (tx.slackChannel or "").strip()
    ts = (tx.slackTs or "").strip()
    if not channel or not ts or not slack_bot_configured():
        return
    try:
        await _slack_api("chat.delete", {"channel": channel, "ts": ts})
    except Exception as exc:
        log.warning("slack_transactions delete failed for transaction %s: %s", tx.id, exc)


async def set_transaction_status(tx: Transaction, status: Literal["approved", "rejected"], by: UserExtra) -> None:
    """Record the decision, then show it in Slack. The one place a transaction gets approved or rejected."""
    tx.status = status
    tx.approvedBy = by.id
    tx.approvedAt = datetime.utcnow()
    await tx.save()
    await announce_decision(tx, by)


# --- Approve / Reject buttons --------------------------------------------------------------------------

# Slack's replay window for signed requests.
_SIGNATURE_MAX_AGE = 300
_ACTIONS: dict[str, Literal["approved", "rejected"]] = {"tx_approve": "approved", "tx_reject": "rejected"}
_RESPONSE_URL_PREFIX = "https://hooks.slack.com/"

NOT_ADMIN = "Only admins can approve or reject transactions in Slack."
NO_TRANSACTION = "This transaction no longer exists."


def is_transaction_action(action_id: str) -> bool:
    return action_id in _ACTIONS


def verify_slack_signature(secret: str, timestamp: str, body: bytes, signature: str, now: float) -> bool:
    """True when `signature` is Slack's v0 HMAC-SHA256 of `v0:{timestamp}:{body}` under `secret` and the timestamp
    is within 5 minutes of `now` (either way). Constant-time compare."""
    if not secret or not timestamp or not signature:
        return False
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    if abs(now - ts) > _SIGNATURE_MAX_AGE:
        return False
    expected = "v0=" + hmac.new(secret.encode(), f"v0:{timestamp}:".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.encode(), signature.encode())


def _display_name(user: Optional[UserExtra]) -> str:
    if user is None:
        return ""
    return _slack_escape((user.name or "").strip() or (user.email or "").strip())


async def handle_transaction_action(action_id: str, tx_id: str, slack_user_id: str) -> Optional[str]:
    """Apply an Approve / Reject click. None on success, else the text to show the clicker (ephemeral)."""
    status = _ACTIONS[action_id]
    slack_user_id = (slack_user_id or "").strip()
    admin = None
    if slack_user_id:
        admin = await UserExtra.find_one({"slackUserId": slack_user_id, "role": "admin", "isActive": {"$ne": False}})
    if admin is None:
        return NOT_ADMIN
    try:
        oid = PydanticObjectId(tx_id)
    except Exception:
        return NO_TRANSACTION
    tx = await Transaction.get(oid)
    if tx is None:
        return NO_TRANSACTION
    if tx.status != "pending":
        word = "approved" if tx.status == "approved" else "rejected"
        by = _display_name(await UserExtra.get(tx.approvedBy)) if tx.approvedBy else ""
        return f"Already {word} by {by}." if by else f"Already {word}."
    await set_transaction_status(tx, status, admin)
    return None


async def _post_ephemeral(response_url: str, payload: dict) -> None:
    """POST to the click's response_url. Only Slack's hooks host is called. Never raises."""
    if not (response_url or "").startswith(_RESPONSE_URL_PREFIX):
        log.warning("slack_transactions ignoring response_url outside %s", _RESPONSE_URL_PREFIX)
        return
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(response_url, json=payload)
    except Exception as exc:
        log.warning("slack_transactions ephemeral reply failed: %s", exc)


async def process_transaction_click(action_id: str, tx_id: str, slack_user_id: str, response_url: str) -> None:
    """Handle a click and tell the clicker when it was refused. Never raises."""
    try:
        text = await handle_transaction_action(action_id, tx_id, slack_user_id)
    except Exception as exc:
        log.warning("slack_transactions click on %s failed: %s", tx_id, exc)
        text = "Something went wrong. Try again from the app."
    if text:
        await _post_ephemeral(response_url, {"response_type": "ephemeral", "replace_original": False, "text": text})
