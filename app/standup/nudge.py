"""The 6 PM standup nudge: one message per weekday in the daily plan channel (no per-person numbers)."""

from __future__ import annotations

import logging
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

from app import slack_bot
from app.config import get_settings
from app.plan_auto import WORKDAYS

log = logging.getLogger(__name__)

STANDUP_WRITE = "standup_write"
NUDGE_AT = time(18, 0)
NUDGE_TZ = ZoneInfo("America/Chicago")


def nudge_blocks(day: date, origin: str) -> tuple[str, list[dict]]:
    text = f"🗒️ Daily standup — {day:%a %m/%d}"
    return text, [
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*{text}*"}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "Time to log today. Your draft is ready: bids, interviews and your plan are filled in."}},
        {"type": "actions", "elements": [
            {"type": "button", "action_id": STANDUP_WRITE, "value": day.isoformat(), "style": "primary",
             "text": {"type": "plain_text", "text": "Write my update"}},
            {"type": "button", "action_id": "standup_open_app", "url": f"{origin.rstrip('/')}/plans?day={day.isoformat()}",
             "text": {"type": "plain_text", "text": "Open in app"}},
        ]},
    ]


async def run_standup_nudge(now: datetime) -> str:
    """Post once per weekday from 18:00 Chicago (claim first, release on failure).

    Returns "off", "skipped", "posted" or "failed"."""
    s = get_settings()
    channel = (s.SLACK_DAILY_PLAN_CHANNEL_ID or "").strip()
    if not channel or not (s.SLACK_BOT_TOKEN or "").strip():
        return "off"
    local = slack_bot._aware_utc(now).astimezone(NUDGE_TZ)
    if local.weekday() >= WORKDAYS or local.time() < NUDGE_AT:
        return "skipped"
    day = local.date()
    key = f"standup-nudge:{day.isoformat()}"
    if not await slack_bot._reminder_claim(key):
        return "skipped"
    try:
        text, blocks = nudge_blocks(day, s.FRONTEND_ORIGIN)
        await slack_bot._slack_api("chat.postMessage", {
            "channel": channel, "text": text, "blocks": blocks, "unfurl_links": False, "unfurl_media": False})
        return "posted"
    except Exception as exc:
        log.warning("standup nudge failed: %s", exc)
        await slack_bot._reminder_release(key)
        return "failed"
