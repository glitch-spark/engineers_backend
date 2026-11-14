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
from app.interview_day.rounds import DayRound, _is_round, load_rounds
from app.models import Interview
from app.models.interview_day_post import InterviewDayPost

log = logging.getLogger(__name__)

# One refresh at a time per process, so two quick interview writes cannot both post the same extra message.
_refresh_lock = asyncio.Lock()


def _now(now: Optional[datetime]) -> datetime:
    return slack_bot._aware_utc(now) if now is not None else datetime.now(timezone.utc)


async def _thread_links(rounds: list[DayRound]) -> None:
    """Fill `thread_link` of each caller round with the permalink of its #caller post (None when it has none).
    An interview's rounds share one thread, so its permalink is fetched once per call."""
    wanted = [r for r in rounds if r.caller != "self"]
    if not wanted:
        return
    ids = {PydanticObjectId(r.interview_id) for r in wanted}
    interviews = {str(iv.id): iv for iv in await Interview.find({"_id": {"$in": list(ids)}}).to_list()}
    links: dict[tuple, Optional[str]] = {}
    for r in wanted:
        iv = interviews.get(r.interview_id)
        entry = next((e for e in iv.stageHistory if e.id == r.round_id), None) if iv else None
        if entry is None or entry.caller is None:
            continue
        # With an interview thread every round links to it; an older round has its own post.
        key = (r.interview_id,) if getattr(iv, "slackThread", None) else (r.interview_id, r.round_id)
        if key not in links:
            links[key] = await slack_bot._caller_daily_permalink(iv, entry)
        r.thread_link = links[key]


async def _build(day: date, tz: ZoneInfo) -> tuple[list[list[dict]], str, list[str]]:
    """The day's messages (blocks), the plain fallback text that goes with each, and the interviews listed."""
    start, end = slack_bot.caller_daily_bounds(day, tz)
    rounds = await load_rounds(start, end)
    await _thread_links(rounds)
    origin = get_settings().FRONTEND_ORIGIN.rstrip("/")
    messages = day_messages(day, rounds, managers=await slack_bot.interview_managers(), origin=origin)
    return messages, fallback_text(day, len(rounds)), sorted({r.interview_id for r in rounds})


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
        messages, text, listed = await _build(day, tz)
        for blocks in messages:
            channel_id, ts = await _post_message(channel, text, blocks)
            channel, posted = channel_id, [*posted, ts]
        await InterviewDayPost(day=day.isoformat(), channel=channel, ts=posted, interviewIds=listed).insert()
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


async def _is_affected(stored: InterviewDayPost, interview_id: str, deleted: bool, day: date, tz: ZoneInfo) -> bool:
    """Whether a write to this interview can change today's post: it is listed in it, or (unless it is gone) now has
    an active round in today's window."""
    if interview_id in stored.interviewIds:
        return True
    if deleted:
        return False
    try:
        iv = await Interview.get(PydanticObjectId(interview_id))
    except Exception:
        return False
    if iv is None:
        return False
    start, end = slack_bot.caller_daily_bounds(day, tz)
    return any(_is_round(e, start, end) for e in iv.stageHistory)


async def refresh_today(
    now: Optional[datetime] = None, *, interview_id: Optional[str] = None, deleted: bool = False,
) -> bool:
    """Edit today's post to match the database: update each stored message, post extra ones, delete surplus ones.

    With `interview_id` (a write to that interview, `deleted` when it or one of its rounds was removed) it does
    nothing unless the post lists that interview or the interview now has a round today.

    False when Slack is off, today has no post (the 05:00 run creates it) or the write is not about today. Raises
    when Slack fails; progress made until then is kept."""
    local = _local_today(now)
    if local is None:
        return False
    day, tz = local
    if await InterviewDayPost.find_one(InterviewDayPost.day == day) is None:
        return False
    async with _refresh_lock:
        # Read inside the lock: an earlier refresh may have just saved the interview into (or out of) the post.
        stored = await InterviewDayPost.find_one(InterviewDayPost.day == day)
        if stored is None:
            return False
        if interview_id is not None and not await _is_affected(
            stored, interview_id, deleted, date.fromisoformat(day), tz,
        ):
            return False
        messages, text, listed = await _build(date.fromisoformat(day), tz)
        channel, ts = stored.channel, list(stored.ts)
        done = False
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
            done = True
        finally:
            # If Slack failed the old messages may still show an interview that has left the list: keep it listed.
            ids = listed if done else sorted(set(stored.interviewIds) | set(listed))
            if ts != stored.ts or ids != stored.interviewIds:
                stored.ts, stored.interviewIds = ts, ids
                await stored.save()
        return True
