"""Team reminder channel: each team's daily list (posted once from its dailyTime, then edited in place) and a
30-minute reminder per round. Mirrors app/interview_day/{post,reminders}.py for the callers' workspace: no buttons,
no app links. Slack trouble is logged (ids only), never raised into the interview save."""

import logging
import math
from datetime import date, datetime, time, timedelta, timezone
from types import SimpleNamespace
from typing import Optional
from zoneinfo import ZoneInfo

from pymongo.errors import DuplicateKeyError

from app import slack_bot
from app.caller_teams import slack
from app.caller_teams.blocks import cc, when, who
from app.caller_teams.sync import STALE_CLAIM, TeamRound, _directory, _utc, team_round
from app.interview_caller import ENDED_STATUSES
from app.interview_day.blocks import MAX_BLOCKS, MAX_NAME, _method_part, _plural, _section, _short
from app.interview_normalize import normalize_status
from app.models.caller import Caller
from app.models.caller_team import CallerTeam, TeamDayPost, TeamRoundPost
from app.models.interview import Interview
from app.slack_bot import _stage_label

log = logging.getLogger(__name__)

NO_INTERVIEWS = "No team interviews today."


async def _rounds_between(lo: datetime, hi: datetime, callers, teams, team_id: Optional[str] = None,
                          exclude: frozenset = frozenset()) -> list[TeamRound]:
    """Live team rounds (not canceled/rejected) with lo <= start < hi, by start; one team's, or every team's."""
    members = [cid for cid, c in callers.items()
               if c.teamId in teams and teams[c.teamId].archivedAt is None and (team_id is None or c.teamId == team_id)]
    if not members:
        return []
    naive = lambda d: d.astimezone(timezone.utc).replace(tzinfo=None)  # noqa: E731
    pad = timedelta(seconds=1)  # Mongo keeps milliseconds; the exact bounds are applied below
    ivs = await Interview.find({"stageHistory": {"$elemMatch": {
        "caller.callerId": {"$in": members}, "scheduledAt": {"$gte": naive(lo - pad), "$lt": naive(hi + pad)},
    }}}).to_list()
    out = []
    for iv in ivs:
        for e in iv.stageHistory:
            tr = team_round(iv, e, callers=callers, teams=teams) if e.id and e.id not in exclude else None
            if (tr is None or tr.start is None or not lo <= tr.start < hi
                    or (team_id and str(tr.team.id) != team_id)
                    or normalize_status(e.status) in ENDED_STATUSES):
                continue
            out.append(tr)
    return sorted(out, key=lambda t: t.start)


async def team_rounds_for_day(team: CallerTeam, day: date, *, directory=None,
                              exclude: frozenset = frozenset()) -> list[TeamRound]:
    """The team's rounds that start on `day` in the team's timezone, by start. TBD rounds are not listed."""
    callers, teams = directory or await _directory()
    lo, hi = slack_bot.caller_daily_bounds(day, ZoneInfo(team.timezone))
    return await _rounds_between(lo, hi, callers, teams, str(team.id), exclude)


def _line(tr: TeamRound) -> str:
    people = f"{who(tr.caller) or '—'}" + cc(tr)
    caller = tr.entry.caller
    method = _method_part(SimpleNamespace(method=caller.method if caller else None,
                                          link=caller.methodValue if caller else ""))
    return (f"*{_short(tr.iv.companyName, MAX_NAME, '—')}* · {_stage_label(tr.entry.stage)} · {when(tr)}"
            + (f" · {method}" if method else "") + f"\nWho: {people}")


def _messages(day: date, rounds: list[TeamRound]) -> tuple[list[list[dict]], str]:
    """The day's messages (at most 50 blocks each) and the fallback text."""
    title = f"Team interviews — {day:%a} {day:%b} {day.day}"
    if not rounds:
        return [[_section(NO_INTERVIEWS)]], f"{title} · {NO_INTERVIEWS}"
    head = {"type": "header", "text": {"type": "plain_text", "text": f"⏰ {title} · {_plural(len(rounds), 'interview')}",
                                       "emoji": True}}
    blocks = [head, *(_section(_line(tr)) for tr in rounds)]
    return ([blocks[i:i + MAX_BLOCKS] for i in range(0, len(blocks), MAX_BLOCKS)],
            f"{title} · {_plural(len(rounds), 'interview')}")


def _at(team: CallerTeam) -> time:
    return time.fromisoformat(team.dailyTime)


async def _edit(post: TeamDayPost, team: CallerTeam, day: date, directory, exclude: frozenset = frozenset()) -> None:
    """Bring the stored messages in line with the day: edit each, post extra ones, delete surplus ones."""
    messages, text = _messages(day, await team_rounds_for_day(team, day, directory=directory, exclude=exclude))
    ts = list(post.ts)
    for i, blocks in enumerate(messages):
        if i < len(ts):
            await slack.api("chat.update", {"channel": post.channel, "ts": ts[i], "text": text, "blocks": blocks})
        else:
            sent = await slack.api("chat.postMessage", {
                "channel": post.channel, "text": text, "blocks": blocks, "unfurl_links": False,
            })
            ts.append(str(sent["ts"]))
    for extra in ts[len(messages):]:
        await slack.api("chat.delete", {"channel": post.channel, "ts": extra})
    ts = ts[:len(messages)]
    if ts != post.ts:
        post.ts = ts
        await post.save()


async def _post_day(team: CallerTeam, day: date, directory) -> str:
    row = TeamDayPost(teamId=str(team.id), day=day.isoformat(), channel=team.reminderChannelId, ts=[])
    try:
        await row.insert()  # the claim: a concurrent tick hits the unique index
    except DuplicateKeyError:
        return "already_sent"
    sent: list[str] = []
    try:
        rounds = await team_rounds_for_day(team, day, directory=directory)
        messages, text = _messages(day, rounds)
        for blocks in messages:
            res = await slack.api("chat.postMessage", {
                "channel": row.channel, "text": text, "blocks": blocks, "unfurl_links": False,
            })
            sent.append(str(res["ts"]))
        row.ts = sent
        row.interviewIds = sorted({str(tr.iv.id) for tr in rounds})
        await row.save()
        return "posted"
    except Exception as exc:
        log.warning("team day post failed (team=%s): %s", team.id, exc)
        for ts in sent:
            try:
                await slack.api("chat.delete", {"channel": row.channel, "ts": ts})
            except Exception as exc2:
                log.warning("team day post cleanup failed (team=%s): %s", team.id, exc2)
        await TeamDayPost.get_motor_collection().delete_one({"_id": row.id})
        return "failed"


async def run_team_day_posts(now: datetime) -> dict[str, str]:
    """Per team: post today's list once from `dailyTime` until the local day ends, else edit it in place.
    Values: "posted" | "edited" | "not_due" | "already_sent" | "failed"."""
    if not slack.callers_configured():
        return {}
    directory = await _directory()
    out: dict[str, str] = {}
    for team in directory[1].values():
        if team.archivedAt is not None:
            continue
        tid = str(team.id)
        try:
            day = slack_bot.caller_daily_due(now, at=_at(team), tz=ZoneInfo(team.timezone))
            if day is None:
                out[tid] = "not_due"
                continue
            post = await TeamDayPost.find_one(TeamDayPost.teamId == tid, TeamDayPost.day == day.isoformat())
            if post is None:
                out[tid] = await _post_day(team, day, directory)
            elif not post.ts:  # a claim whose poster died: take it over once stale
                created = _utc(post.createdAt)
                if created < datetime.now(timezone.utc) - STALE_CLAIM:
                    await TeamDayPost.get_motor_collection().delete_one({"_id": post.id, "ts": []})
                out[tid] = "already_sent"
            else:
                await _edit(post, team, day, directory)
                out[tid] = "edited"
        except Exception as exc:
            log.warning("team day post failed (team=%s): %s", tid, exc)
            out[tid] = "failed"
    return out


async def refresh_team_day(team_id: str, now: datetime, deleted: frozenset = frozenset()) -> None:
    """Edit the team's posted day list after an interview write. Never posts (before dailyTime nothing is out yet)
    and never raises."""
    try:
        if not slack.callers_configured():
            return
        directory = await _directory()
        team = directory[1].get(str(team_id))
        if team is None or team.archivedAt is not None:
            return
        day = _utc(now).astimezone(ZoneInfo(team.timezone)).date()
        post = await TeamDayPost.find_one(TeamDayPost.teamId == str(team.id), TeamDayPost.day == day.isoformat())
        if post is not None and post.ts:
            await _edit(post, team, day, directory, deleted)
    except Exception as exc:
        log.warning("team day refresh failed (team=%s): %s", team_id, exc)


async def _thread_link(tr: TeamRound, round_id: str) -> Optional[str]:
    post = await TeamRoundPost.find_one(TeamRoundPost.teamId == str(tr.team.id), TeamRoundPost.roundId == round_id)
    if post is None or not post.ts:
        return None
    try:
        return (await slack.api("chat.getPermalink", {"channel": post.channel, "message_ts": post.ts})).get("permalink")
    except Exception as exc:
        log.info("team reminder permalink failed (team=%s round=%s): %s", tr.team.id, round_id, exc)
        return None


def _reminder_text(tr: TeamRound, minutes: int, link: Optional[str]) -> str:
    lines = [f"⏰ Starting in {minutes} min — *{_short(tr.iv.companyName, MAX_NAME, '—')}* · "
             f"{_stage_label(tr.entry.stage)}", f"When: {when(tr)}", f"Who: {who(tr.caller) or '—'}{cc(tr)}"]
    if link:
        lines.append(f"<{link}|Open the interview thread>")
    return "\n".join(lines)


async def run_team_reminders(now: datetime) -> dict[str, int]:
    """Remind each team round once per start time, when it starts in (now, now + 30 min]: claim first, release on
    failure, so a rescheduled round gets a new reminder."""
    sent = failed = 0
    if not slack.callers_configured():
        return {"sent": sent, "failed": failed}
    now = slack_bot._aware_utc(now)
    last = now + slack_bot.CALLER_REMINDER_LEAD
    callers, teams = await _directory()
    for tr in await _rounds_between(now, last + timedelta(microseconds=1), callers, teams):
        if tr.start <= now:
            continue
        rid = tr.entry.id
        key = f"team-reminder:{tr.team.id}:{rid}:{int(tr.start.timestamp())}"
        if not await slack_bot._reminder_claim(key):
            continue
        try:
            minutes = max(1, math.ceil((tr.start - now).total_seconds() / 60))
            text = _reminder_text(tr, minutes, await _thread_link(tr, rid))
            await slack.api("chat.postMessage", {
                "channel": tr.team.reminderChannelId, "text": text, "blocks": [_section(text)],
                "unfurl_links": False, "unfurl_media": False,
            })
            sent += 1
        except Exception as exc:
            failed += 1
            log.warning("team reminder failed (team=%s round=%s interview=%s): %s", tr.team.id, rid, tr.iv.id, exc)
            try:
                await slack_bot._reminder_release(key)
            except Exception as exc2:
                log.warning("team reminder release failed (team=%s round=%s): %s", tr.team.id, rid, exc2)
    return {"sent": sent, "failed": failed}
