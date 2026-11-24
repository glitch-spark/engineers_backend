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
from app.caller_teams import files as team_files
from app.caller_teams import slack
from app.caller_teams.blocks import cc, when, who
from app.caller_teams.sync import STALE_CLAIM, TeamRound, _directory, _utc, team_round
from app.interview_caller import ENDED_STATUSES
from app.interview_day.blocks import MAX_LINK, MAX_NAME, _method_part, _plural, _section, _short
from app.interview_normalize import normalize_status
from app.models.account import Account
from app.models.caller import Caller
from app.models.caller_team import CallerTeam, TeamDayPost, TeamRoundPost
from app.models.interview import Interview
from app.slack_bot import _mrkdwn_link, _stage_label

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


def _clock(dt: datetime, zone: ZoneInfo) -> str:
    return dt.astimezone(zone).strftime("%I:%M %p").lstrip("0")


def _meeting_time(tr: TeamRound) -> str:
    """"12:00 PM - 12:30 PM PST, Nov 24" in the team's timezone; no end time when the round has none."""
    zone = ZoneInfo(tr.team.timezone)
    local = tr.start.astimezone(zone)
    end = tr.entry.endsAt
    end = (end if end.tzinfo else end.replace(tzinfo=timezone.utc)) if end else None
    span = _clock(tr.start, zone) + (f" - {_clock(end, zone)}" if end and end > tr.start else "")
    return f"{span} {local.strftime('%Z')}, {local.strftime('%b')} {local.day}"


def _bare(url: str) -> str:
    """A URL without its scheme or trailing slash, for the link label."""
    return url.split("://", 1)[-1].rstrip("/")


def _line(no: int, tr: TeamRound, profile: Optional[str]) -> str:
    """One numbered round in the day list, a field per line."""
    caller = tr.entry.caller
    lines = [f"{no}. Who: {who(tr.caller) or '—'}{cc(tr)}",
             f"Profile name: {_short(profile, MAX_NAME, '—')}",
             f"Company name: {_short(tr.iv.companyName, MAX_NAME, '—')}",
             f"Round: {_stage_label(tr.entry.stage)}"]
    job = (tr.iv.jobUrl or (tr.iv.resumeLink.jobUrl if tr.iv.resumeLink else None) or "").strip()
    if job:
        lines.append(f"Job link: {_mrkdwn_link(job, _bare(job)[:MAX_LINK])}")
    link = ((caller.methodValue if caller else "") or "").strip()
    if link.startswith(("http://", "https://")) and len(link) <= MAX_LINK:
        lines.append(f"Meeting link: {_mrkdwn_link(link, _bare(link))}")
    else:
        method = _method_part(SimpleNamespace(method=caller.method if caller else None, link=link))
        if method:
            lines.append(f"Call: {method}")
    lines.append(f"Meeting time: {_meeting_time(tr)}")
    return "\n".join(lines)


async def _profiles(rounds: list[TeamRound]) -> dict[str, str]:
    """Profile (account) name per interview account id."""
    ids = {tr.iv.accountId for tr in rounds if tr.iv.accountId}
    if not ids:
        return {}
    return {str(a.id): a.name for a in await Account.find({"_id": {"$in": list(ids)}}).to_list()}


DAY_READY_ASK = ("Please react with :white_check_mark: once you've reviewed today's interviews and are ready for "
                 "your calls.")


def _header(day: date, rounds: list[TeamRound]) -> tuple[str, list[dict]]:
    title = f"Team interviews — {day:%a} {day:%b} {day.day}"
    count = _plural(len(rounds), "interview") if rounds else NO_INTERVIEWS
    text = f"⏰ {title} · {count}"
    return text, [{"type": "header", "text": {"type": "plain_text", "text": text, "emoji": True}}]


def _round_message(no: int, tr: TeamRound, profile: Optional[str]) -> tuple[str, list[dict]]:
    body = f"{_line(no, tr, profile)}\n_{DAY_READY_ASK}_"
    return f"{_short(tr.iv.companyName, MAX_NAME, '—')} · {_stage_label(tr.entry.stage)}", [_section(body)]


def _gone_message() -> tuple[str, list[dict]]:
    text = "~This interview is no longer on today's list.~"
    return text, [_section(text)]


async def _claim(post: TeamDayPost, field: str, rid: str) -> bool:
    """Claim a round's message (`rounds`) or files (`filesDone`) on the stored row, so concurrent runs act once."""
    col = TeamDayPost.get_motor_collection()
    if field == "rounds":
        res = await col.update_one({"_id": post.id, f"rounds.{rid}": {"$exists": False}}, {"$set": {f"rounds.{rid}": ""}})
    else:
        res = await col.update_one({"_id": post.id, "filesDone": {"$ne": rid}}, {"$push": {"filesDone": rid}})
    return res.modified_count == 1


async def _release(post: TeamDayPost, field: str, rid: str) -> None:
    col = TeamDayPost.get_motor_collection()
    op = {"$unset": {f"rounds.{rid}": ""}} if field == "rounds" else {"$pull": {"filesDone": rid}}
    await col.update_one({"_id": post.id}, op)


async def _round_files(post: TeamDayPost, ts: str, tr: TeamRound) -> None:
    rid = tr.entry.id
    if not await _claim(post, "filesDone", rid):
        return
    try:
        files, note = await team_files.round_files(tr.iv, rid, tr.team.timezone)
        files = [(team_files.clean_name(name) or "file.pdf", data) for name, data in files]
        comment = "Brief and resume for this call." if note is None else f"Brief for this call. {note}"
        await slack.upload_files(post.channel, ts, files, comment)
    except Exception as exc:
        log.warning("team day files failed (team=%s round=%s): %r", post.teamId, rid, exc)
        await _release(post, "filesDone", rid)


async def _sync_day(post: TeamDayPost, team: CallerTeam, day: date, directory,
                    exclude: frozenset = frozenset(), now: Optional[datetime] = None) -> None:
    """Header edited in place; one message per round (posted once, then edited) with its files in the thread; a
    round that left the day keeps its message (and thread), marked as no longer listed."""
    now = _utc(now or datetime.now(timezone.utc))
    rounds = await team_rounds_for_day(team, day, directory=directory, exclude=exclude)
    profiles = await _profiles(rounds)
    text, blocks = _header(day, rounds)
    await slack.api("chat.update", {"channel": post.channel, "ts": post.ts[0], "text": text, "blocks": blocks})
    for extra in post.ts[1:]:  # chunks of an older one-message list
        await slack.api("chat.delete", {"channel": post.channel, "ts": extra})
    if len(post.ts) > 1:
        await TeamDayPost.get_motor_collection().update_one({"_id": post.id}, {"$set": {"ts": post.ts[:1]}})
    listed = set()
    for no, tr in enumerate(rounds, 1):
        rid = tr.entry.id
        listed.add(rid)
        text, blocks = _round_message(no, tr, profiles.get(str(tr.iv.accountId)))
        fresh = await TeamDayPost.get(post.id)
        ts = fresh.rounds.get(rid) if fresh else None
        if ts:
            await slack.api("chat.update", {"channel": post.channel, "ts": ts, "text": text, "blocks": blocks})
        elif ts is None and await _claim(post, "rounds", rid):
            try:
                ts = str((await slack.api("chat.postMessage", {
                    "channel": post.channel, "text": text, "blocks": blocks, "unfurl_links": False,
                }))["ts"])
            except Exception:
                await _release(post, "rounds", rid)
                raise
            await TeamDayPost.get_motor_collection().update_one({"_id": post.id}, {"$set": {f"rounds.{rid}": ts}})
        # ponytail: a poster that dies between claim and post leaves "" for good; that round then has no message
        if ts and tr.start > now and rid not in (fresh.filesDone if fresh else []):
            await _round_files(post, ts, tr)
    fresh = await TeamDayPost.get(post.id)
    for rid, ts in (fresh.rounds if fresh else {}).items():
        if ts and rid not in listed:
            text, blocks = _gone_message()
            await slack.api("chat.update", {"channel": post.channel, "ts": ts, "text": text, "blocks": blocks})
    ids = sorted({str(tr.iv.id) for tr in rounds} | set(post.interviewIds))
    if ids != post.interviewIds:
        await TeamDayPost.get_motor_collection().update_one({"_id": post.id}, {"$set": {"interviewIds": ids}})


def _at(team: CallerTeam) -> time:
    return time.fromisoformat(team.dailyTime)


async def _post_day(team: CallerTeam, day: date, directory, now: datetime) -> str:
    row = TeamDayPost(teamId=str(team.id), day=day.isoformat(), channel=team.reminderChannelId, ts=[])
    try:
        await row.insert()  # the claim: a concurrent tick hits the unique index
    except DuplicateKeyError:
        return "already_sent"
    try:
        rounds = await team_rounds_for_day(team, day, directory=directory)
        text, blocks = _header(day, rounds)
        res = await slack.api("chat.postMessage", {
            "channel": row.channel, "text": text, "blocks": blocks, "unfurl_links": False,
        })
        row.ts = [str(res["ts"])]
        await row.save()
    except Exception as exc:
        log.warning("team day post failed (team=%s): %s", team.id, exc)
        await TeamDayPost.get_motor_collection().delete_one({"_id": row.id})
        return "failed"
    try:
        await _sync_day(row, team, day, directory, now=now)
    except Exception as exc:  # the header is out; the next tick posts what is missing
        log.warning("team day rounds failed (team=%s): %r", team.id, exc)
    return "posted"


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
                out[tid] = await _post_day(team, day, directory, now)
            elif not post.ts:  # a claim whose poster died: take it over once stale
                created = _utc(post.createdAt)
                if created < datetime.now(timezone.utc) - STALE_CLAIM:
                    await TeamDayPost.get_motor_collection().delete_one({"_id": post.id, "ts": []})
                out[tid] = "already_sent"
            else:
                await _sync_day(post, team, day, directory, now=now)
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
            await _sync_day(post, team, day, directory, deleted, now=now)
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


async def _day_thread(tr: TeamRound, round_id: str) -> Optional[str]:
    """The ts of the round's own message in today's team day list, when it has one."""
    day = tr.start.astimezone(ZoneInfo(tr.team.timezone)).date().isoformat()
    post = await TeamDayPost.find_one(TeamDayPost.teamId == str(tr.team.id), TeamDayPost.day == day)
    return (post.rounds.get(round_id) or None) if post else None


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
            day_ts = await _day_thread(tr, rid)
            text = _reminder_text(tr, minutes, None if day_ts else await _thread_link(tr, rid))
            body = {"channel": tr.team.reminderChannelId, "text": text, "blocks": [_section(text)],
                    "unfurl_links": False, "unfurl_media": False}
            if day_ts:
                body["thread_ts"] = day_ts
            await slack.api("chat.postMessage", body)
            sent += 1
        except Exception as exc:
            failed += 1
            log.warning("team reminder failed (team=%s round=%s interview=%s): %s", tr.team.id, rid, tr.iv.id, exc)
            try:
                await slack_bot._reminder_release(key)
            except Exception as exc2:
                log.warning("team reminder release failed (team=%s round=%s): %s", tr.team.id, rid, exc2)
    return {"sent": sent, "failed": failed}
