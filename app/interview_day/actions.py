"""Slack button clicks on the interview day post."""

from __future__ import annotations

import logging

from app.models import UserExtra
from app.round_confirm import NOT_ALLOWED, ConfirmError, confirm_round
from app.slack_transactions import _post_ephemeral

log = logging.getLogger(__name__)


async def _reply(response_url: str, text: str) -> None:
    await _post_ephemeral(response_url, {"response_type": "ephemeral", "replace_original": False, "text": text})


async def process_confirm_click(value: str, slack_user_id: str, response_url: str) -> None:
    """The Confirm button: `value` is "<interviewId>:<roundId>". Only an active platform user with this Slack id can
    confirm, and `confirm_round` applies the caller / manager / owner rule. A success sends no message (the day post
    refreshes). Refusals reach the clicker as an ephemeral reply. Never raises."""
    try:
        slack_id = (slack_user_id or "").strip()
        user = (
            await UserExtra.find_one({"slackUserId": slack_id, "isActive": {"$ne": False}}) if slack_id else None
        )
        if user is None:
            await _reply(response_url, NOT_ALLOWED)
            return
        iv_id, _, round_id = (value or "").partition(":")
        if not iv_id or not round_id:
            raise ConfirmError(404, "not found")
        await confirm_round(iv_id, round_id, user, True)
    except ConfirmError as exc:
        await _reply(response_url, exc.message)
    except Exception as exc:
        log.warning("interview confirm click %r failed: %s", value, exc)
        await _reply(response_url, "Something went wrong. Try again from the app.")
