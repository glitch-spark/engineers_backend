"""The day post: every round of the local day as Block Kit in the daily channel, posted at 05:00 and edited in
place whenever an interview changes. One `InterviewDayPost` per day remembers the message ts values."""

from __future__ import annotations

import asyncio
import logging
from datetime import date, datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from beanie import PydanticObjectId

from app import slack_bot
from app.config import get_settings
from app.interview_day.blocks import day_messages, fallback_text
from app.interview_day.rounds import DayRound, load_rounds
from app.models import Interview
from app.models.interview_day_post import InterviewDayPost

log = logging.getLogger(__name__)

# One refresh at a time per process, so two quick interview writes cannot both post the same extra message.
_refresh_lock = asyncio.Lock()


def _now(now: Optional[datetime]) -> datetime:
    return slack_bot._aware_utc(now) if now is not None else datetime.now(timezone.utc)


async def _thread_links(rounds: list[DayRound]) -> None:
    """Fill `thread_link` of each caller round with the permalink of its #caller post (None when it has none)."""
    wanted = [r for r in rounds if r.caller != "self"]
    if not wanted:
        return
    ids = {PydanticObjectId(r.interview_id) for r in wanted}
    interviews = {str(iv.id): iv for iv in await Interview.find({"_id": {"$in": list(ids)}}).to_list()}
    for r in wanted:
        iv = interviews.get(r.interview_id)
        entry = next((e for e in iv.stageHistory if e.id == r.round_id), None) if iv else None
        if entry is not None and entry.caller is not None:
            r.thread_link = await slack_bot._caller_daily_permalink(iv, entry)


async def _build(day: date, tz: ZoneInfo) -> tuple[list[list[dict]], str]:
    """The day's messages (blocks) and the plain fallback text that goes with each."""
    start, end = slack_bot.caller_daily_bounds(day, tz)
    rounds = await load_rounds(start, end)
    await _thread_links(rounds)
    origin = get_settings().FRONTEND_ORIGIN.rstrip("/")
    messages = day_messages(day, rounds, managers=await slack_bot.interview_managers(), origin=origin)
    return messages, fallback_text(day, len(rounds))


def _body(channel: str, text: str, blocks: list[dict]) -> dict:
    return {"channel": channel, "text": text, "blocks": blocks, "unfurl_links": False, "unfurl_media": False}


async def _post_message(channel: str, text: str, blocks: list[dict]) -> tuple[str, str]:
    """Post one message; returns (channel id, ts). Slack answers with the channel's id even when we sent a name."""
    sent = await slack_bot._slack_api("chat.postMessage", _body(channel, text, blocks))
    ts = str(sent.get("ts") or "")
    if not ts:
        raise RuntimeError("chat.postMessage returned no ts")
    return str(sent.get("channel") or channel), ts


async def run_day_post(now: datetime) -> str:
    """Post the day's interviews once, from the configured time on (claim first, release on failure).

    Returns "posted", "not_due", "already_sent", "off" or "failed"."""
    cfg = slack_bot._caller_daily_configured()
    if cfg is None:
        return "off"
    channel, at, tz = cfg
    day = slack_bot.caller_daily_due(now, at=at, tz=tz)
    if day is None:
        return "not_due"
    key = f"interview-day:{day.isoformat()}"
    if not await slack_bot._reminder_claim(key):
        return "already_sent"
    posted: list[str] = []
    keep = False
    try:
        if await InterviewDayPost.find_one(InterviewDayPost.day == day.isoformat()) is not None:
            keep = True  # the post is there, only its claim row was lost
            return "already_sent"
        messages, text = await _build(day, tz)
        for blocks in messages:
            channel_id, ts = await _post_message(channel, text, blocks)
            channel, posted = channel_id, [*posted, ts]
        await InterviewDayPost(day=day.isoformat(), channel=channel, ts=posted).insert()
        keep = True
        return "posted"
    except Exception as exc:
        log.warning("interview day post failed: %s", exc)
        return "failed"
    finally:
        if not keep:
            # Nothing is stored for a half-posted day, so take the partial messages back before the retry.
            for ts in posted:
                try:
                    await slack_bot._slack_api("chat.delete", {"channel": channel, "ts": ts})
                except Exception as exc:
                    log.warning("interview day post cleanup of %s failed: %s", ts, exc)
            await slack_bot._reminder_release(key)


def _local_today(now: Optional[datetime]) -> Optional[tuple[str, ZoneInfo]]:
    cfg = slack_bot._caller_daily_configured()
    if cfg is None:
        return None
    tz = cfg[2]
    return _now(now).astimezone(tz).date().isoformat(), tz


async def today_parent_ts(now: datetime) -> Optional[str]:
    """The first message of today's post (what #caller reminders thread under), None when it is not posted."""
    local = _local_today(now)
    if local is None:
        return None
    stored = await InterviewDayPost.find_one(InterviewDayPost.day == local[0])
    return stored.ts[0] if stored is not None and stored.ts else None


async def refresh_today(now: Optional[datetime] = None) -> bool:
    """Edit today's post to match the database: update each stored message, post extra ones, delete surplus ones.

    False when Slack is off or today has no post (the 05:00 run creates it). Raises when Slack fails; progress made
    until then is kept."""
    local = _local_today(now)
    if local is None:
        return False
    day, tz = local
    async with _refresh_lock:
        stored = await InterviewDayPost.find_one(InterviewDayPost.day == day)
        if stored is None:
            return False
        messages, text = await _build(date.fromisoformat(day), tz)
        channel, ts = stored.channel, list(stored.ts)
        try:
            for i, blocks in enumerate(messages):
                if i < len(ts):
                    try:
                        await slack_bot._slack_api(
                            "chat.update", {**_body(channel, text, blocks), "ts": ts[i]},
                        )
                        continue
                    except RuntimeError as exc:
                        if str(exc) not in slack_bot._MESSAGE_GONE:
                            raise
                    ts[i] = (await _post_message(channel, text, blocks))[1]  # Slack lost it: post it afresh
                else:
                    ts.append((await _post_message(channel, text, blocks))[1])
            while len(ts) > len(messages):
                gone = ts[-1]
                try:
                    await slack_bot._slack_api("chat.delete", {"channel": channel, "ts": gone})
                except RuntimeError as exc:
                    if str(exc) not in slack_bot._MESSAGE_GONE:
                        log.warning("interview day post delete of %s failed: %s", gone, exc)
                ts.pop()
        finally:
            if ts != stored.ts:
                stored.ts = ts
                await stored.save()
        return True
