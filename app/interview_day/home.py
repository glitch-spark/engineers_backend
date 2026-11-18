"""A round's home message in the daily channel (spec 2026-11-17-interview-followups-daily-channel-design.md): the
30-minute reminder is the round's own top-level message, and the after-interview check and the 1-week result
reminder reply in its thread. A round with no home (added too late for a reminder, from before this shipped, or its
message deleted in Slack) gets one from its first follow-up, posted top-level."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from app import slack_bot
from app.models.round_slack_post import RoundSlackPost


def start_epoch(start: datetime) -> int:
    return int(slack_bot._aware_utc(start).timestamp())


async def find_home(round_id: str, start: datetime) -> Optional[RoundSlackPost]:
    return await RoundSlackPost.find_one({"roundId": round_id, "startEpoch": start_epoch(start)})


async def record_home(round_id: str, interview_id: str, start: datetime, channel: str, ts: str) -> None:
    """Upsert the round's home for this start."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    await RoundSlackPost.get_motor_collection().update_one(
        {"roundId": round_id, "startEpoch": start_epoch(start)},
        {"$set": {"interviewId": interview_id, "channel": channel, "ts": ts, "updatedAt": now},
         "$setOnInsert": {"createdAt": now}},
        upsert=True,
    )


async def post_in_home(
    round_id: str, interview_id: str, start: datetime, channel: str, text: str, blocks: list[dict],
) -> str:
    """Reply in the round's home thread; with no home, or one Slack no longer has, post top-level and make that the
    home. Returns the posted message's ts. Other Slack errors raise."""
    body = {"channel": channel, "text": text, "blocks": blocks, "unfurl_links": False, "unfurl_media": False}
    found = await find_home(round_id, start)
    if found is not None:
        try:
            sent = await slack_bot._slack_api("chat.postMessage", {**body, "channel": found.channel,
                                                                   "thread_ts": found.ts})
            return str(sent.get("ts") or "")
        except RuntimeError as exc:
            if str(exc) not in slack_bot._MESSAGE_GONE | {"thread_not_found"}:
                raise
    sent = await slack_bot._slack_api("chat.postMessage", body)
    ts = str(sent.get("ts") or "")
    if ts:
        await record_home(round_id, interview_id, start, channel, ts)
    return ts
