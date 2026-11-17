"""Slack button clicks on the interview day post and its reminders."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from app.interview_day.reminders import rebuild_reminder
from app.models import UserExtra
from app.models.interview_day_post import InterviewDayPost
from app.round_confirm import NOT_ALLOWED, ConfirmError, confirm_round
from app.slack_transactions import _post_ephemeral

log = logging.getLogger(__name__)

CONFIRMED_NOTE = "Confirmed — thanks."


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _reply(response_url: str, text: str) -> None:
    await _post_ephemeral(response_url, {"response_type": "ephemeral", "replace_original": False, "text": text})


async def _show_confirmed(iv_id: str, round_id: str, response_url: str, message_ts: str) -> None:
    """After a confirm, make the clicked message say so. A reminder is replaced by its round rebuilt (confirmed, no
    Confirm button); a click on the day post needs nothing, since `confirm_round` refreshes it and replacing it with
    one round would wipe the others. When the reminder cannot be rebuilt the clicker still hears it worked."""
    if not message_ts:
        return
    try:
        if await InterviewDayPost.find_one({"ts": message_ts}) is not None:
            return
        text, blocks = await rebuild_reminder(iv_id, round_id, _now())
    except Exception as exc:
        log.warning("interview confirm click: rebuilding %s:%s failed: %s", iv_id, round_id, exc)
        await _reply(response_url, CONFIRMED_NOTE)
        return
    await _post_ephemeral(
        response_url, {"response_type": "in_channel", "replace_original": True, "text": text, "blocks": blocks},
    )


async def process_confirm_click(value: str, slack_user_id: str, response_url: str, message_ts: str = "") -> None:
    """The Confirm button: `value` is "<interviewId>:<roundId>", `message_ts` the clicked message. Only an active
    platform user with this Slack id can confirm, and `confirm_round` applies the caller / manager / owner rule.
    A success updates the clicked reminder (see `_show_confirmed`); refusals reach the clicker as an ephemeral
    reply. Never raises."""
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
        await _show_confirmed(iv_id, round_id, response_url, (message_ts or "").strip())
    except ConfirmError as exc:
        await _reply(response_url, exc.message)
    except Exception as exc:
        log.warning("interview confirm click %r failed: %s", value, exc)
        await _reply(response_url, "Something went wrong. Try again from the app.")
