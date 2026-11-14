"""30-minute reminders: one reply per round in today's day post thread, built like the round's block in the post."""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta

from app import slack_bot
from app.config import get_settings
from app.interview_day.blocks import round_blocks
from app.interview_day.post import _thread_links, today_parent_ts
from app.interview_day.rounds import DayRound, load_rounds
from app.slack_bot import _stage_label, _text

log = logging.getLogger(__name__)


def reminder_key(r: DayRound) -> str:
    """One reminder per round and start time, so a rescheduled round gets a new one."""
    return f"round-reminder:{r.round_id}:{int(r.start.timestamp())}"


def _minutes(r: DayRound, now: datetime) -> int:
    return max(1, math.ceil((r.start - now).total_seconds() / 60))


def _fallback(r: DayRound, minutes: int) -> str:
    """Plain text for notifications and old clients."""
    return f"Starting in {minutes} min — {_text(r.company)} · {_stage_label(r.stage)}"


async def _send(channel: str, parent: str | None, text: str, blocks: list[dict]) -> None:
    body = {"channel": channel, "text": text, "blocks": blocks, "unfurl_links": False, "unfurl_media": False}
    try:
        await slack_bot._slack_api("chat.postMessage", {**body, "thread_ts": parent} if parent else body)
    except RuntimeError as exc:
        # The day post is gone from Slack: remind in the channel instead.
        if not parent or str(exc) not in slack_bot._MESSAGE_GONE | {"thread_not_found"}:
            raise
        await slack_bot._slack_api("chat.postMessage", body)


async def run_round_reminders(now: datetime) -> dict[str, int]:
    """Remind each round once, when it starts in (now, now + 30 min] (claim first, release on failure).

    Off without the daily channel. Replies under today's day post, else posts in the channel."""
    sent = failed = 0
    cfg = slack_bot._caller_daily_configured()
    if cfg is None:
        return {"sent": sent, "failed": failed}
    channel = cfg[0]
    now = slack_bot._aware_utc(now)
    # load_rounds' end is exclusive and Mongo keeps milliseconds only, so ask a second further and filter here.
    last = now + slack_bot.CALLER_REMINDER_LEAD
    due = [r for r in await load_rounds(now, last + timedelta(seconds=1)) if now < r.start <= last]
    if not due:
        return {"sent": sent, "failed": failed}
    managers = await slack_bot.interview_managers()
    parent = await today_parent_ts(now)
    origin = get_settings().FRONTEND_ORIGIN.rstrip("/")
    for r in due:
        key = reminder_key(r)
        if not await slack_bot._reminder_claim(key):
            continue
        ok = False
        try:
            await _thread_links([r])
            minutes = _minutes(r, now)
            blocks = round_blocks(r, managers=managers, origin=origin, starting_in=minutes)
            await _send(channel, parent, _fallback(r, minutes), blocks)
            ok = True
        except Exception as exc:
            log.warning("interview reminder failed for round %s: %s", r.round_id, exc)
        if ok:
            sent += 1
        else:
            failed += 1
            await slack_bot._reminder_release(key)
    return {"sent": sent, "failed": failed}
