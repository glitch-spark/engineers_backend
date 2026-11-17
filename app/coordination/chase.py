"""The coordination channel's chasers: the after-call ask and the 7-day result reminder, both thread replies under
the interview's post. Off until SLACK_CALLER_ASKS_FROM is set; only caller rounds that END after it are chased.

Claims reuse SlackAlertLog (`slack_bot._reminder_claim`, claim before posting, release on failure):
"after-call:<roundId>:<startEpoch>" and "result-reminder:<roundId>"."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional
from zoneinfo import ZoneInfo

from app import slack_bot
from app.config import get_settings
from app.coordination.blocks import ask_blocks, result_reminder_blocks
from app.interview_normalize import interview_status_value, normalize_stage
from app.models import Interview, SlackAlertLog, UserExtra
from app.models.interview import InterviewStage, InterviewStageEvent
from app.routers.interviews import round_ends_at

log = logging.getLogger(__name__)

CHASE_TZ = ZoneInfo("America/Chicago")
RESULT_REMINDER_AFTER = timedelta(days=7)
_ASKS_FROM_FORMAT = "%Y-%m-%dT%H:%M"


def asks_from() -> Optional[datetime]:
    """The release cutoff as aware UTC (the setting is Chicago wall-clock time); None when unset."""
    raw = (get_settings().SLACK_CALLER_ASKS_FROM or "").strip()
    if not raw:
        return None
    return datetime.strptime(raw, _ASKS_FROM_FORMAT).replace(tzinfo=CHASE_TZ).astimezone(timezone.utc)


@dataclass
class ChaseRound:
    interview: Interview
    entry: InterviewStageEvent
    start: datetime  # aware UTC
    end: datetime  # aware UTC
    owner: Optional[UserExtra]


def _status_query(status: str) -> Any:
    # Stored values, not normalized ones: a legacy "rescheduled" and a missing status (None also matches a missing
    # field) both read as scheduled.
    return {"$in": ["scheduled", "rescheduled", None]} if status == "scheduled" else status


async def caller_rounds(status: str, *, ended_after: datetime, ended_by: datetime) -> list[ChaseRound]:
    """Caller rounds (caller on, not a home assessment, the interview has a post) whose normalized status is
    `status` and whose end is in (ended_after, ended_by]."""
    ended_after, ended_by = slack_bot._aware_utc(ended_after), slack_bot._aware_utc(ended_by)
    found = await Interview.find({
        "slackThread": {"$ne": None},
        "stageHistory": {"$elemMatch": {
            "caller.enabled": True, "status": _status_query(status),
            "scheduledAt": {"$lte": ended_by.replace(tzinfo=None)},
        }},
    }).to_list()
    rows: list[tuple[Interview, InterviewStageEvent, datetime, datetime]] = []
    for iv in found:
        for entry in iv.stageHistory:
            if entry.scheduledAt is None or not (entry.caller and entry.caller.enabled):
                continue
            if normalize_stage(entry.stage) == InterviewStage.home_assessment.value:
                continue
            if (interview_status_value(entry.status) or "scheduled") != status:
                continue
            start = slack_bot._aware_utc(entry.scheduledAt)
            end = slack_bot._aware_utc(round_ends_at(start, entry.endsAt))
            if ended_after < end <= ended_by:
                rows.append((iv, entry, start, end))
    if not rows:
        return []
    owner_ids = list({iv.createdBy for iv, *_ in rows})
    owners = {str(u.id): u for u in await UserExtra.find({"_id": {"$in": owner_ids}}).to_list()}
    return [ChaseRound(iv, e, start, end, owners.get(str(iv.createdBy))) for iv, e, start, end in rows]


def after_call_key(c: ChaseRound) -> str:
    """One ask per round and start time, so a rescheduled round is asked again after its new end."""
    return f"after-call:{c.entry.id}:{int(c.start.timestamp())}"


def result_reminder_key(c: ChaseRound) -> str:
    return f"result-reminder:{c.entry.id}"


def _active_cutoff() -> Optional[datetime]:
    """The cutoff when the chasers are on (a channel, a bot token and a cutoff are all set), else None."""
    s = get_settings()
    if not (s.SLACK_CALLER_CHANNEL_ID or "").strip() or not (s.SLACK_BOT_TOKEN or "").strip():
        return None
    return asks_from()


async def _post(channel: str, thread_ts: str, text: str, blocks: list[dict]) -> None:
    body = {"channel": channel, "text": text, "blocks": blocks, "unfurl_links": False, "unfurl_media": False}
    try:
        await slack_bot._slack_api("chat.postMessage", {**body, "thread_ts": thread_ts})
    except RuntimeError as exc:
        # The interview's post is gone from Slack: ask in the channel instead.
        if str(exc) not in slack_bot._MESSAGE_GONE | {"thread_not_found"}:
            raise
        await slack_bot._slack_api("chat.postMessage", body)


async def _chase(
    rounds: list[ChaseRound], key: Callable[[ChaseRound], str], build: Callable[[ChaseRound], tuple[str, list[dict]]],
) -> dict[str, int]:
    sent = failed = 0
    if not rounds:
        return {"sent": sent, "failed": failed}
    keys = [key(c) for c in rounds]
    # One read for the keys already sent, so they cost no insert.
    done = {r.kind for r in await SlackAlertLog.find({
        "userId": slack_bot.CALLER_CHANNEL_SENTINEL, "kind": {"$in": keys},
    }).to_list()}
    for c, k in zip(rounds, keys):
        if k in done or not await slack_bot._reminder_claim(k):
            continue
        ok = False
        try:
            text, blocks = build(c)
            thread = c.interview.slackThread
            await _post(thread.channel, thread.ts, text, blocks)
            ok = True
        except Exception as exc:
            log.warning("coordination chase %s failed: %s", k, exc)
        if ok:
            sent += 1
        else:
            failed += 1
            try:
                await slack_bot._reminder_release(k)
            except Exception as exc:
                log.warning("coordination chase release of %s failed: %s", k, exc)
    return {"sent": sent, "failed": failed}


def _ref(c: ChaseRound) -> str:
    return f"{c.interview.id}:{c.entry.id}"


def _ask(c: ChaseRound) -> tuple[str, list[dict]]:
    return ask_blocks(_ref(c), owner=c.owner, company=c.interview.companyName or "",
                      stage=normalize_stage(c.entry.stage) or "", end=c.end)


def _reminder(c: ChaseRound) -> tuple[str, list[dict]]:
    return result_reminder_blocks(_ref(c), owner=c.owner, company=c.interview.companyName or "",
                                  stage=normalize_stage(c.entry.stage) or "", start=c.start)


async def run_after_call_asks(now: datetime) -> dict[str, int]:
    """Ask the owner how a round went, once, after the round has ended."""
    cutoff = _active_cutoff()
    if cutoff is None:
        return {"sent": 0, "failed": 0}
    rounds = await caller_rounds("scheduled", ended_after=cutoff, ended_by=now)
    return await _chase(rounds, after_call_key, _ask)


async def run_result_reminders(now: datetime) -> dict[str, int]:
    """Remind the owner once when a round has been completed for 7 days with no result."""
    cutoff = _active_cutoff()
    if cutoff is None:
        return {"sent": 0, "failed": 0}
    rounds = await caller_rounds("completed", ended_after=cutoff, ended_by=now - RESULT_REMINDER_AFTER)
    return await _chase(rounds, result_reminder_key, _reminder)


async def run_coordination_chase(now: datetime) -> dict[str, Any]:
    """The tick's coordination step. Each part is isolated; a crash reports zero for that part."""
    parts: dict[str, Callable[[datetime], Awaitable[dict[str, int]]]] = {
        "after_call_asks": run_after_call_asks, "result_reminders": run_result_reminders,
    }
    out: dict[str, Any] = {}
    for name, run in parts.items():
        try:
            out[name] = (await run(now))["sent"]
        except Exception as exc:
            log.exception("coordination chase %s crashed: %s", name, exc)
            out[name] = 0
    return out
