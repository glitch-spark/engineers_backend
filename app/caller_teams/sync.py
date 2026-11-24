"""Team channel "Interview scheduled" posts: one top-level post per team round in the team's channel, kept in line
with the round (spec "Team channel" change table). Driven by interview saves (`sync_team_posts`, in the background)
and by the 5-minute tick (`sweep_team_posts`). State lives on `TeamRoundPost`; every write to it is a compare-and-set,
so concurrent syncs post, reply and upload at most once. Slack trouble is logged, never raised."""

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

from beanie import PydanticObjectId
from pymongo.errors import DuplicateKeyError

from app.caller_teams import files as team_files
from app.caller_teams import slack
from app.caller_teams.blocks import reply_text, scheduled_blocks
from app.interview_caller import ENDED_STATUSES
from app.interview_normalize import normalize_status
from app.models.caller import Caller
from app.models.caller_team import CallerTeam, TeamRoundPost
from app.models.interview import Interview, InterviewStageEvent

log = logging.getLogger(__name__)

# A claim (post row with no ts yet) older than this is from a process that died before posting: it may be taken over.
STALE_CLAIM = timedelta(minutes=2)


@dataclass
class TeamRound:
    team: CallerTeam
    caller: Optional[Caller]
    manager: Optional[Caller]
    iv: Optional[Interview]
    entry: Optional[InterviewStageEvent]  # None: the round was deleted (only when rendering a "moved off" post)
    start: Optional[datetime]


def _utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def _epoch(dt: Optional[datetime]) -> Optional[int]:
    return None if dt is None else int(dt.timestamp())


def team_round(iv, entry, *, callers: dict[str, Caller], teams: dict[str, CallerTeam]) -> Optional[TeamRound]:
    """The round as a team round: an enabled external caller whose team exists and is not archived; else None."""
    c = getattr(entry, "caller", None)
    if c is None or not c.enabled or c.callerKind != "external" or not c.callerId:
        return None
    caller = callers.get(c.callerId)
    team = teams.get(caller.teamId or "") if caller else None
    if team is None or team.archivedAt is not None:
        return None
    return TeamRound(team, caller, callers.get(team.managerCallerId), iv, entry, _utc(entry.scheduledAt))


def _digest(text: str, blocks: list[dict]) -> str:
    return hashlib.sha1(json.dumps([text, blocks], sort_keys=True).encode()).hexdigest()


def _ctx(post_or_ids) -> str:
    """Log context: ids only (never the token or personal data)."""
    p = post_or_ids
    return f"interview={p.interviewId} round={p.roundId} team={p.teamId}"


async def _cas(post: TeamRoundPost, **new) -> bool:
    """Set `new` on the row only if the fields still hold what `post` read; True when written (or nothing to do)."""
    changed = {k: v for k, v in new.items() if getattr(post, k) != v}
    if not changed:
        return True
    old = {k: getattr(post, k) for k in changed}
    res = await TeamRoundPost.get_motor_collection().update_one(
        {"_id": post.id, **old}, {"$set": {**changed, "updatedAt": datetime.utcnow()}},
    )
    if not res.modified_count:
        return False
    for k, v in changed.items():
        setattr(post, k, v)
    return True


async def _post_new(tr: TeamRound, round_id: str, now: datetime) -> str:
    text, blocks = scheduled_blocks(tr, "scheduled")
    post = TeamRoundPost(
        teamId=str(tr.team.id), interviewId=str(tr.iv.id), roundId=round_id, channel=tr.team.channelId,
        callerId=str(tr.caller.id), startEpoch=_epoch(tr.start), state="scheduled", digest=_digest(text, blocks),
    )
    try:
        await post.insert()  # the claim: a concurrent sync hits the unique index
    except DuplicateKeyError:
        return "skipped"
    try:
        ts = (await slack.api("chat.postMessage", {
            "channel": post.channel, "text": text, "blocks": blocks, "unfurl_links": False,
        }))["ts"]
    except Exception as exc:
        log.warning("team post failed (%s): %s", _ctx(post), exc)
        await TeamRoundPost.get_motor_collection().delete_one({"_id": post.id, "ts": ""})
        return "skipped"
    await _cas(post, ts=ts)
    if not tr.caller.slackUserId:
        log.info("team post: caller has no Slack ID, shown by name (%s)", _ctx(post))
    await _send_files(post, tr, now)
    return "posted"


async def _send_files(post: TeamRoundPost, tr: TeamRound, now: datetime) -> bool:
    """Brief + resume into the post's thread, once per start (-1 = a TBD round); never for a past round. The
    `filesFor` claim is released on failure so the sweep retries."""
    want = post.startEpoch if post.startEpoch is not None else -1
    if post.state != "scheduled" or not post.ts or post.filesFor == want:
        return False
    if tr.start is not None and tr.start <= now:
        return False
    old = post.filesFor
    if not await _cas(post, filesFor=want):
        return False
    try:
        files, note = await team_files.round_files(tr.iv, post.roundId, tr.team.timezone)
        files = [(team_files.clean_name(name) or "file.pdf", data) for name, data in files]
        comment = "Brief and resume for this call." if note is None else f"Brief for this call. {note}"
        await slack.upload_files(post.channel, post.ts, files, comment)
    except Exception as exc:
        log.warning("team post files failed (%s): %s", _ctx(post), exc)
        await _cas(post, filesFor=old)
        return False
    return True


async def _apply(post: TeamRoundPost, text: str, blocks: list[dict], lines: list[str], **new) -> bool:
    """Write the new state, then edit the post (if its render changed) and reply in its thread; a Slack failure
    puts the old state back so the next sync retries."""
    digest = _digest(text, blocks)
    old = {k: getattr(post, k) for k in (*new, "digest")}
    if not await _cas(post, digest=digest, **new):
        return False
    try:
        if digest != old["digest"]:
            await slack.api("chat.update", {"channel": post.channel, "ts": post.ts, "text": text, "blocks": blocks})
        if lines:
            await slack.api("chat.postMessage", {
                "channel": post.channel, "thread_ts": post.ts, "text": "\n".join(lines), "unfurl_links": False,
            })
    except Exception as exc:
        log.warning("team post update failed (%s): %s", _ctx(post), exc)
        await _cas(post, **old)
        return False
    return True


async def _mark_off(post: TeamRoundPost, iv, entry, callers, teams) -> str:
    """The round left this team (another caller, TBD/Self/off, the caller left, the team archived, round deleted):
    the post says so and the previous caller + manager are tagged."""
    team = teams.get(post.teamId)
    if team is None:  # teams are archived, never deleted; nothing to render against
        return "off" if await _cas(post, state="off") else "skipped"
    tr = TeamRound(team, callers.get(post.callerId), callers.get(team.managerCallerId), iv, entry,
                   _utc(entry.scheduledAt) if entry is not None else None)
    text, blocks = scheduled_blocks(tr, "off")
    ok = await _apply(post, text, blocks, [reply_text("off", tr, previous=tr.caller)], state="off")
    return "off" if ok else "skipped"


async def _update(post: TeamRoundPost, tr: TeamRound, ended: bool, now: datetime) -> str:
    state = "canceled" if ended else "scheduled"
    caller_id, start = str(tr.caller.id), _epoch(tr.start)
    lines: list[str] = []
    if ended:
        if post.state != "canceled":
            lines.append(reply_text("canceled", tr))
    elif post.state == "off":
        lines.append(reply_text("back", tr))
    elif post.state == "canceled":
        lines.append(reply_text("reinstated", tr))
    else:
        if post.callerId != caller_id:
            lines.append(reply_text("assigned", tr))
        if post.startEpoch != start:
            lines.append(reply_text("time_set" if post.startEpoch is None else "moved", tr))
    new = {"callerId": caller_id, "startEpoch": start, "state": state}
    if post.state == "off" and not ended:
        new["filesFor"] = None  # back on the team: the files go again
    text, blocks = scheduled_blocks(tr, state)
    edited = _digest(text, blocks) != post.digest
    if not await _apply(post, text, blocks, lines, **new):
        return "skipped"
    sent = await _send_files(post, tr, now)
    if lines:
        return "canceled" if ended else "replied"
    return "edited" if edited else ("replied" if sent else "none")


async def sync_round(iv, entry, round_id: str, now: datetime, *, callers: dict[str, Caller],
                     teams: dict[str, CallerTeam]) -> str:
    """Bring one round's team post(s) in line: "posted" | "edited" | "replied" | "canceled" | "off" | "none" |
    "skipped" (lost a race, a past round, or Slack failed: the sweep retries)."""
    tr = team_round(iv, entry, callers=callers, teams=teams) if entry is not None else None
    team_id = str(tr.team.id) if tr else None
    action = "none"
    current = None
    for post in await TeamRoundPost.find(TeamRoundPost.roundId == round_id).to_list():
        if post.teamId == team_id:
            current = post
        elif post.state != "off" and post.ts:
            action = await _mark_off(post, iv, entry, callers, teams)
    if tr is None:
        return action
    if current is not None and not current.ts:
        created = _utc(current.createdAt)
        if created > datetime.now(timezone.utc) - STALE_CLAIM:
            return "skipped"  # another sync is posting it right now
        log.warning("team post: taking over a stale claim (%s)", _ctx(current))
        await TeamRoundPost.get_motor_collection().delete_one({"_id": current.id, "ts": ""})
        current = None
    ended = normalize_status(entry.status) in ENDED_STATUSES
    if current is None:
        if ended:
            return action
        if tr.start is not None and tr.start <= now:
            return "skipped"  # only upcoming rounds are ever posted
        return await _post_new(tr, round_id, now)
    return await _update(current, tr, ended, now)


async def _directory() -> tuple[dict[str, Caller], dict[str, CallerTeam]]:
    callers = {str(c.id): c for c in await Caller.find_all().to_list()}
    teams = {str(t.id): t for t in await CallerTeam.find_all().to_list()}
    return callers, teams


async def _sync_interview(interview_id: str, now: datetime, deleted: set[str], callers, teams) -> list[str]:
    iv = await Interview.get(PydanticObjectId(interview_id))
    rounds = {e.id: e for e in (iv.stageHistory if iv else []) if e.id and e.id not in deleted}
    posted = {p.roundId for p in await TeamRoundPost.find(TeamRoundPost.interviewId == interview_id).to_list()}
    out = []
    for rid in [*rounds, *sorted(posted - set(rounds))]:
        entry = rounds.get(rid)
        if rid not in posted and team_round(iv, entry, callers=callers, teams=teams) is None:
            continue
        try:
            out.append(await sync_round(iv, entry, rid, now, callers=callers, teams=teams))
        except Exception as exc:
            log.exception("team post sync failed (interview=%s round=%s): %s", interview_id, rid, exc)
            out.append("failed")
    return out


async def sync_team_posts(interview_id: str, *, now: Optional[datetime] = None, deleted: Iterable[str] = ()) -> None:
    """Sync every team post of one interview (rounds in `deleted` count as gone). Never raises."""
    if not slack.callers_configured():
        return
    try:
        await _sync_interview(str(interview_id), now or datetime.now(timezone.utc), set(deleted), *await _directory())
    except Exception as exc:
        log.exception("team posts sync failed (interview=%s): %s", interview_id, exc)


async def sweep_team_posts(now: datetime) -> dict[str, int]:
    """The tick's catch-up: every interview with an upcoming round that is a team round or has a live post.
    Returns how many rounds got each action."""
    if not slack.callers_configured():
        return {}
    callers, teams = await _directory()
    live = {tid for tid, t in teams.items() if t.archivedAt is None}
    members = [cid for cid, c in callers.items() if c.teamId in live]
    naive_now = now.astimezone(timezone.utc).replace(tzinfo=None)
    ids: set[str] = set()
    if members:
        cursor = Interview.get_motor_collection().find({"stageHistory": {"$elemMatch": {
            "caller.enabled": True, "caller.callerId": {"$in": members},
            "$or": [{"scheduledAt": {"$gt": naive_now}}, {"scheduledAt": None}],
        }}}, {"_id": 1})
        ids |= {str(d["_id"]) for d in await cursor.to_list(None)}
    posts = await TeamRoundPost.find({
        "state": {"$ne": "off"}, "$or": [{"startEpoch": {"$gt": int(now.timestamp())}}, {"startEpoch": None}],
    }).to_list()
    ids |= {p.interviewId for p in posts}
    counts: dict[str, int] = {}
    for iid in sorted(ids):
        try:
            actions = await _sync_interview(iid, now, set(), callers, teams)
        except Exception as exc:
            log.exception("team posts sweep failed (interview=%s): %s", iid, exc)
            actions = ["failed"]
        for a in actions:
            counts[a] = counts.get(a, 0) + 1
    return counts
