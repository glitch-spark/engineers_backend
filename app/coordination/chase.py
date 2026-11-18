"""The interview follow-ups (spec 2026-11-17-interview-followups-daily-channel-design.md): the after-interview check
(15 minutes after a round ends) and the 7-day result reminder, for caller and self rounds, each a reply in the round's
home thread in the daily channel (app/interview_day/home.py). Off until SLACK_CALLER_ASKS_FROM is set; only rounds
that END after it, and at most 2 days before the check is due, are chased.

The Friday result roundup is no longer posted; a roundup already in Slack is still edited in place as its rounds are
answered (`refresh_latest_roundup`).

Claims reuse SlackAlertLog (`slack_bot._reminder_claim`, claim before posting, release on failure):
"after-call:<roundId>:<startEpoch>" and "result-reminder:<roundId>"."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional
from zoneinfo import ZoneInfo

from beanie import PydanticObjectId

from app import slack_bot
from app.config import get_settings
from app.coordination.blocks import RoundupRow, ask_blocks, result_reminder_blocks, roundup_blocks
from app.interview_day.home import post_in_home
from app.interview_normalize import interview_status_value, normalize_stage
from app.models import Account, Interview, ResultRoundupPost, SlackAlertLog, UserExtra
from app.models.interview import InterviewStage, InterviewStageEvent
from app.routers.interviews import round_ends_at

log = logging.getLogger(__name__)

CHASE_TZ = ZoneInfo("America/Chicago")
RESULT_REMINDER_AFTER = timedelta(days=7)
ASK_DELAY = timedelta(minutes=15)
# Rounds that ended (for the reminder: whose 7-day mark passed) longer ago are skipped: no backlog on deploy day.
MAX_AGE = timedelta(days=2)
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
    # Stored values, not normalized ones; the Python filter on the read status is the real check. A missing status
    # (None also matches a missing field) reads as scheduled while the round is ahead and as completed once it is
    # past (app/interview_normalize.py), and a legacy "rescheduled" reads as scheduled.
    return {"$in": ["scheduled", "rescheduled", None]} if status == "scheduled" else {"$in": [status, None]}


async def ended_rounds(status: str, *, ended_after: datetime, ended_by: datetime) -> list[ChaseRound]:
    """Rounds, caller or self (not home assessments), whose normalized status is `status` and whose end is in
    (ended_after, ended_by]."""
    ended_after, ended_by = slack_bot._aware_utc(ended_after), slack_bot._aware_utc(ended_by)
    found = await Interview.find({
        "stageHistory": {"$elemMatch": {
            "status": _status_query(status),
            "scheduledAt": {"$lte": ended_by.replace(tzinfo=None)},
            # Bound below so the set does not grow with every round ever called. A round's end is endsAt when that is
            # after the start, else start + 1 h, so it ends after `ended_after` only if it starts within the hour
            # before it or its endsAt is after it. Looser than the Python filter, never tighter.
            "$or": [
                {"scheduledAt": {"$gt": (ended_after - timedelta(hours=1)).replace(tzinfo=None)}},
                {"endsAt": {"$gt": ended_after.replace(tzinfo=None)}},
            ],
        }},
    }).to_list()
    rows: list[tuple[Interview, InterviewStageEvent, datetime, datetime]] = []
    for iv in found:
        for entry in iv.stageHistory:
            if entry.scheduledAt is None:
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


def _daily_channel() -> Optional[str]:
    cfg = slack_bot._caller_daily_configured()
    return cfg[0] if cfg is not None else None


def _active_cutoff() -> Optional[datetime]:
    """The cutoff when the chasers are on (the daily channel, a bot token and a cutoff are all set), else None."""
    if _daily_channel() is None or not (get_settings().SLACK_BOT_TOKEN or "").strip():
        return None
    return asks_from()


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
            await post_in_home(str(c.entry.id), str(c.interview.id), c.start, _daily_channel() or "", text, blocks)
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
    """Ask the owner how a round went, once, 15 minutes after the round has ended."""
    cutoff = _active_cutoff()
    if cutoff is None:
        return {"sent": 0, "failed": 0}
    now = slack_bot._aware_utc(now)
    rounds = await ended_rounds("scheduled", ended_after=max(cutoff, now - MAX_AGE),
                                ended_by=now - ASK_DELAY)
    return await _chase(rounds, after_call_key, _ask)


async def run_result_reminders(now: datetime) -> dict[str, int]:
    """Remind the owner once when a round has been completed for 7 days with no result."""
    cutoff = _active_cutoff()
    if cutoff is None:
        return {"sent": 0, "failed": 0}
    now = slack_bot._aware_utc(now)
    rounds = await ended_rounds("completed", ended_after=max(cutoff, now - RESULT_REMINDER_AFTER - MAX_AGE),
                                ended_by=now - RESULT_REMINDER_AFTER)
    return await _chase(rounds, result_reminder_key, _reminder)


def _roundup_message(
    rounds: list[ChaseRound], profiles: dict[str, str], friday: date,
) -> tuple[str, list[dict], list[str]]:
    """`(text, blocks, refs)`: owners by display name, each owner's rounds by start."""
    by_owner: dict[str, tuple[Optional[UserExtra], list[ChaseRound]]] = {}
    for c in rounds:
        by_owner.setdefault(str(c.interview.createdBy), (c.owner, []))[1].append(c)
    ordered = sorted(by_owner.values(), key=lambda g: (g[0] is None, (getattr(g[0], "name", "") or "").lower()))
    groups: list[tuple[Optional[UserExtra], list[RoundupRow]]] = []
    refs: list[str] = []
    for owner, mine in ordered:
        rows = []
        for c in sorted(mine, key=lambda c: c.start):
            rows.append(RoundupRow(
                ref=_ref(c), company=c.interview.companyName or "", stage=normalize_stage(c.entry.stage) or "",
                start=c.start, profile=profiles.get(str(c.interview.accountId), ""),
            ))
            refs.append(_ref(c))
        groups.append((owner, rows))
    text, blocks = roundup_blocks(groups, friday=friday, origin=slack_bot._frontend_link(""))
    return text, blocks, refs


async def _profiles(rounds: list[ChaseRound]) -> dict[str, str]:
    ids = list({c.interview.accountId for c in rounds if c.interview.accountId})
    if not ids:
        return {}
    accounts = await Account.find({"_id": {"$in": ids}}).to_list()
    return {str(a.id): slack_bot._profile_label(a) for a in accounts}


def _rendered(blocks: list[dict]) -> str:
    return hashlib.sha1(json.dumps(blocks, sort_keys=True).encode("utf-8")).hexdigest()


async def _open_rounds(refs: list[str]) -> list[ChaseRound]:
    """The rounds of `refs` that still exist and are still completed."""
    ids = []
    for r in refs:
        try:
            ids.append(PydanticObjectId(r.partition(":")[0]))
        except Exception:
            continue
    found = {str(iv.id): iv for iv in await Interview.find({"_id": {"$in": ids}}).to_list()} if ids else {}
    keep: list[tuple[Interview, InterviewStageEvent]] = []
    for r in refs:
        iv_id, _, round_id = r.partition(":")
        iv = found.get(iv_id)
        entry = next((e for e in iv.stageHistory if e.id == round_id), None) if iv else None
        if entry is None or entry.scheduledAt is None:
            continue
        if (interview_status_value(entry.status) or "scheduled") != "completed":
            continue
        keep.append((iv, entry))
    owner_ids = list({iv.createdBy for iv, _ in keep})
    owners = {str(u.id): u for u in await UserExtra.find({"_id": {"$in": owner_ids}}).to_list()} if owner_ids else {}
    out = []
    for iv, entry in keep:
        start = slack_bot._aware_utc(entry.scheduledAt)
        end = slack_bot._aware_utc(round_ends_at(start, entry.endsAt))
        out.append(ChaseRound(iv, entry, start, end, owners.get(str(iv.createdBy))))
    return out


async def refresh_roundup(post: ResultRoundupPost) -> bool:
    """Re-render the roundup from the refs it listed, minus the rounds answered since, and edit the message when the
    render changed. Returns whether it did."""
    rounds = await _open_rounds(post.refs)
    text, blocks, _ = _roundup_message(rounds, await _profiles(rounds), date.fromisoformat(post.friday))
    digest = _rendered(blocks)
    if digest == post.rendered:
        return False
    try:
        await slack_bot._slack_api("chat.update", {
            "channel": post.channel, "ts": post.ts, "text": text, "blocks": blocks,
            "unfurl_links": False, "unfurl_media": False,
        })
    except RuntimeError as exc:
        if str(exc) not in slack_bot._MESSAGE_GONE:
            raise
        # Deleted in Slack: forget it so the tick stops retrying. Nothing is re-posted (the Friday's claim stays).
        await post.delete()
        log.info("coordination roundup %s (channel %s ts %s) is gone from Slack (%s); no longer edited",
                 post.friday, post.channel, post.ts, exc)
        return False
    post.rendered = digest
    await post.save()
    return True


async def refresh_latest_roundup() -> None:
    """Keep the latest roundup current (an answer given in the app leaves it within one tick). Off with the chasers."""
    if _active_cutoff() is None:
        return
    post = await ResultRoundupPost.find_all().sort("-friday").first_or_none()
    if post is not None:
        await refresh_roundup(post)


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
    try:
        await refresh_latest_roundup()
    except Exception as exc:
        log.warning("coordination chase roundup refresh failed: %s", exc)
    return out
