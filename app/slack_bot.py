"""Slack interview bot: OAuth identity link + private DMs.

Uses the workspace Bot Token for chat.postMessage DMs. Users connect via
Slack OAuth (user_scope=identity.basic) so we store their Slack member id
on UserExtra. Channel posts: the opt-in "Caller needed" message in the
private SLACK_CALLER_CHANNEL_ID, and each daily plan in
SLACK_DAILY_PLAN_CHANNEL_ID (edited/deleted in place as the plan changes).

Sends an instant DM when an interview is created or meaningfully updated,
plus a daily digest at each user's chosen time/timezone that also lists
calls the user joins as a coworker. A round with a caller gets a reply in its
#caller thread ~30 minutes before it starts, tagging the coworkers and owner.
"""

from __future__ import annotations

import logging
import math
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import httpx
from beanie import PydanticObjectId

from app.interview_caller import ENDED_STATUSES, caller_change_lines, round_caller_snapshot
from app.interview_normalize import interview_status_value

from app.config import get_settings
from app.know_your_why import know_your_why_for_date
from app.models import Account, Interview, UserExtra
from app.models.interview import InterviewStatus
from app.models.slack_alert_log import SlackAlertLog
from app.slack_timezones import (
    format_gmt_offset,
    normalize_slack_timezone,
    validate_timezone,
    zoneinfo_for_slack,
)

log = logging.getLogger(__name__)

_SLACK_AUTHORIZE = "https://slack.com/oauth/v2/authorize"
_SLACK_TOKEN = "https://slack.com/api/oauth.v2.access"
_SLACK_API = "https://slack.com/api"
_USER_SCOPE = "identity.basic"


def slack_bot_configured() -> bool:
    return bool((get_settings().SLACK_BOT_TOKEN or "").strip())


def slack_oauth_configured() -> bool:
    s = get_settings()
    return bool(
        (s.SLACK_CLIENT_ID or "").strip()
        and (s.SLACK_CLIENT_SECRET or "").strip()
    )


def resolve_user_tz(user: Optional[UserExtra] = None) -> ZoneInfo:
    """IANA timezone for Slack digest from the fixed GMT-offset allowlist."""
    if user is not None and (user.slackTimezone or "").strip():
        return zoneinfo_for_slack(user.slackTimezone)
    app = (get_settings().APP_TIMEZONE or "").strip()
    return zoneinfo_for_slack(app or None)


def _frontend_link(path: str) -> str:
    return f"{get_settings().FRONTEND_ORIGIN.rstrip('/')}{path}"


def build_oauth_authorize_url(*, state: str) -> str:
    s = get_settings()
    if not slack_oauth_configured():
        raise RuntimeError("Slack OAuth is not configured")
    params = {
        "client_id": s.SLACK_CLIENT_ID,
        "user_scope": _USER_SCOPE,
        "redirect_uri": s.SLACK_OAUTH_REDIRECT_URI,
        "state": state,
    }
    return f"{_SLACK_AUTHORIZE}?{urlencode(params)}"


async def exchange_oauth_code(code: str) -> str:
    """Exchange OAuth code → Slack user id (U…). Raises on failure."""
    s = get_settings()
    if not slack_oauth_configured():
        raise RuntimeError("Slack OAuth is not configured")
    data = {
        "client_id": s.SLACK_CLIENT_ID,
        "client_secret": s.SLACK_CLIENT_SECRET,
        "code": code,
        "redirect_uri": s.SLACK_OAUTH_REDIRECT_URI,
    }
    async with httpx.AsyncClient(timeout=20.0) as client:
        resp = await client.post(_SLACK_TOKEN, data=data)
    payload = resp.json() if resp.content else {}
    if not payload.get("ok"):
        raise RuntimeError(payload.get("error") or f"oauth.v2.access HTTP {resp.status_code}")
    authed = payload.get("authed_user") or {}
    uid = (authed.get("id") or "").strip()
    if not uid:
        raise RuntimeError("Slack OAuth response missing authed_user.id")
    return uid


async def _slack_api(method: str, body: dict[str, Any]) -> dict[str, Any]:
    token = (get_settings().SLACK_BOT_TOKEN or "").strip()
    if not token:
        raise RuntimeError("SLACK_BOT_TOKEN is not configured")
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            f"{_SLACK_API}/{method}",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            json=body,
        )
    payload = resp.json() if resp.content else {}
    if not payload.get("ok"):
        raise RuntimeError(payload.get("error") or f"{method} HTTP {resp.status_code}")
    return payload


async def send_dm(slack_user_id: str, *, text: str, blocks: Optional[list[dict]] = None) -> bool:
    """Open (or reuse) a DM and post. Returns True on success."""
    if not slack_user_id.strip():
        return False
    try:
        opened = await _slack_api("conversations.open", {"users": slack_user_id.strip()})
        channel = (opened.get("channel") or {}).get("id")
        if not channel:
            raise RuntimeError("conversations.open returned no channel")
        payload: dict[str, Any] = {"channel": channel, "text": text}
        if blocks:
            payload["blocks"] = blocks
        await _slack_api("chat.postMessage", payload)
        return True
    except Exception as exc:
        log.warning("slack_bot DM to %s failed: %s", slack_user_id, exc)
        return False


def _fmt_scheduled(dt: Optional[datetime], tz: ZoneInfo) -> str:
    """Calendar date as M/D/YY plus the zone abbrev for that date."""
    if dt is None:
        return "—"
    naive = dt.replace(tzinfo=None) if dt.tzinfo is not None else dt
    day = naive.date()
    local = datetime(day.year, day.month, day.day, 12, 0, 0, tzinfo=tz)
    abbrev = (local.strftime("%Z") or "").strip() or "UTC"
    return f"{day.month}/{day.day}/{day.strftime('%y')} {abbrev}"


def _stage_label(stage: Any) -> str:
    key = _enum_key(stage)
    if not key:
        return "—"
    if key == "ai_interview":
        return "AI Interview"
    return " ".join(part.capitalize() for part in key.split("_"))


def _mrkdwn_link(url: Optional[str], label: str) -> str:
    href = (url or "").strip()
    safe = (label or "").replace("|", " ").replace(">", "").strip() or "link"
    if href.startswith("http://") or href.startswith("https://"):
        return f"<{href}|{safe}>"
    return safe


def _country_flag(code: Optional[str]) -> str:
    """Regional-indicator flag emoji from ISO alpha-2 (e.g. "US" → 🇺🇸)."""
    cc = (code or "").strip().upper()
    if len(cc) != 2 or not cc.isalpha():
        return ""
    return "".join(chr(127397 + ord(ch)) for ch in cc)


def _profile_label(account: Optional[Account]) -> str:
    """Account display name only (no flag/region)."""
    if account is None:
        return ""
    return (account.name or "").strip()


async def _profile_labels(interviews: list[Interview]) -> dict[str, str]:
    account_ids = {iv.accountId for iv in interviews if iv.accountId}
    if not account_ids:
        return {}
    accounts = await Account.find({"_id": {"$in": list(account_ids)}}).to_list()
    return {str(a.id): _profile_label(a) for a in accounts}


def _interview_fields(
    interview: Interview,
    *,
    profile: str,
    tz: ZoneInfo,
) -> str:
    company = (getattr(interview, "companyName", None) or "").strip() or "—"
    job_url = (getattr(interview, "jobUrl", None) or "").strip() or None
    company_value = _mrkdwn_link(job_url, company) if job_url else company
    position = (getattr(interview, "appliedPosition", None) or "").strip() or "—"
    if job_url:
        position = f"{position} ({_mrkdwn_link(job_url, 'Job description')})"
    return "\n".join(
        [
            f"*Profile Name:* {profile.strip() or '—'}",
            f"*Company Name:* {company_value}",
            f"*Applied Position:* {position}",
            f"*Interview Stage:* {_stage_label(getattr(interview, 'stage', None))}",
            f"*Scheduled Time:* {_fmt_scheduled(getattr(interview, 'scheduledAt', None), tz)}",
        ]
    )


def _interview_blocks(
    title: str,
    interviews: list[Interview],
    *,
    why: Optional[str] = None,
    profiles: Optional[dict[str, str]] = None,
    tz: Optional[ZoneInfo] = None,
    include_open_link: bool = False,
) -> list[dict[str, Any]]:
    profiles = profiles or {}
    zone = tz or ZoneInfo("UTC")
    chunks = []
    for iv in interviews:
        profile = profiles.get(str(iv.accountId)) if iv.accountId else ""
        chunks.append(_interview_fields(iv, profile=profile or "", tz=zone))
    body = "\n\n".join(chunks) if chunks else "_No interviews._"
    blocks: list[dict[str, Any]] = [
        {"type": "header", "text": {"type": "plain_text", "text": title, "emoji": True}},
        {"type": "section", "text": {"type": "mrkdwn", "text": body}},
    ]
    if include_open_link:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"<{_frontend_link('/interviews')}|Open interviews>",
                },
            }
        )
    if why:
        blocks.extend(
            [
                {"type": "divider"},
                {
                    "type": "context",
                    "elements": [
                        {
                            "type": "mrkdwn",
                            "text": f"*Know your why*\n_{why}_",
                        }
                    ],
                },
            ]
        )
    return blocks


async def _already_sent(user_id: PydanticObjectId, kind: str) -> bool:
    return await SlackAlertLog.find_one(
        SlackAlertLog.userId == user_id,
        SlackAlertLog.kind == kind,
    ) is not None


async def _mark_sent(
    user_id: PydanticObjectId,
    kind: str,
    *,
    interview_id: Optional[PydanticObjectId] = None,
) -> None:
    try:
        await SlackAlertLog(
            userId=user_id,
            kind=kind,
            interviewId=interview_id,
            sentAt=datetime.now(timezone.utc),
        ).insert()
    except Exception as exc:
        log.debug("slack_bot mark_sent race kind=%s: %s", kind, exc)


def digest_slot_open(
    now_local: datetime,
    *,
    hour: int,
    minute: int,
    window_minutes: int = 5,
) -> bool:
    """True if now is within [digest_time, digest_time + window)."""
    slot = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return slot <= now_local < slot + timedelta(minutes=window_minutes)


def calendar_day_bounds(day: date) -> tuple[datetime, datetime]:
    """Naive [day 00:00, next day 00:00) for date-only scheduledAt (stored as T12:00)."""
    start = datetime(day.year, day.month, day.day, 0, 0, 0)
    end = start + timedelta(days=1)
    return start, end


async def _linked_users() -> list[UserExtra]:
    return await UserExtra.find(
        {"slackUserId": {"$nin": [None, ""]}, "slackAlertsEnabled": {"$ne": False}}
    ).to_list()


_SKIP_STATUSES = {
    InterviewStatus.completed,
    InterviewStatus.passed,
    InterviewStatus.rejected,
    InterviewStatus.canceled,
}


def interview_dm_eligible(user: Optional[UserExtra]) -> bool:
    """True if this user should receive interview bot DMs."""
    if user is None:
        return False
    if not (user.slackUserId or "").strip():
        return False
    if user.slackAlertsEnabled is False:
        return False
    return True


def _enum_key(value: Any) -> Optional[str]:
    if value is None:
        return None
    raw = getattr(value, "value", value)
    text = str(raw).strip()
    return text or None


async def notify_interview_dm(
    interview: Interview,
    *,
    kind: str = "created",
) -> bool:
    """Best-effort private DM to the interview owner. Never raises."""
    try:
        if not slack_bot_configured():
            return False
        owner = await UserExtra.get(interview.createdBy)
        if not interview_dm_eligible(owner):
            return False
        slack_id = (owner.slackUserId or "").strip()
        title = "New interview" if kind == "created" else "Interview updated"
        tz = resolve_user_tz(owner)
        when = _fmt_scheduled(interview.scheduledAt, tz)
        company = (interview.companyName or "Interview").strip()
        profiles = await _profile_labels([interview])
        blocks = _interview_blocks(
            title,
            [interview],
            profiles=profiles,
            tz=tz,
        )
        text = f"{title}: {company} on {when}"
        return await send_dm(slack_id, text=text, blocks=blocks)
    except Exception:
        log.exception("slack_bot notify_interview_dm crashed kind=%s", kind)
        return False


_METHOD_LABELS = {
    "video": "Video meeting",
    "phone_hushed": "Phone (Hushed)",
    "phone_slynumber": "Phone (Slynumber)",
}


def fmt_caller_time(starts_at: Optional[datetime], tz: ZoneInfo, *, with_day: bool = True) -> str:
    """"Tue 9/29 10:30 AM EDT (GMT−4:00)", or "TBD" without an exact start."""
    if starts_at is None:
        return "TBD"
    aware = starts_at if starts_at.tzinfo else starts_at.replace(tzinfo=timezone.utc)
    local = aware.astimezone(tz)
    clock = local.strftime("%I:%M %p").lstrip("0")
    abbrev = (local.strftime("%Z") or "").strip()
    if not with_day:
        return f"{clock} {abbrev}".strip()
    gmt = format_gmt_offset(local.utcoffset() or timedelta(0))
    day = f"{local.strftime('%a')} {local.month}/{local.day}"
    return f"{day} {clock} {abbrev} ({gmt})"


# Plain-text fallback zone for Slack date tags (notifications, old clients).
_FALLBACK_TZ = ZoneInfo("America/New_York")


def slack_time(starts_at: Optional[datetime], *, with_day: bool = True) -> str:
    """A Slack date tag: every reader sees the time in their own Slack time zone."""
    if starts_at is None:
        return "TBD"
    aware = starts_at if starts_at.tzinfo else starts_at.replace(tzinfo=timezone.utc)
    token = "{date_short_pretty} {time}" if with_day else "{time}"
    fallback = fmt_caller_time(aware, _FALLBACK_TZ, with_day=with_day)
    return f"<!date^{int(aware.timestamp())}^{token}|{fallback}>"


def _method_text(caller: Any) -> str:
    label = _METHOD_LABELS.get(getattr(caller, "method", None) or "", "")
    value = (getattr(caller, "methodValue", "") or "").strip()
    if value.startswith("http://") or value.startswith("https://"):
        value = _mrkdwn_link(value, value)
    if label and value:
        return f"{label} — {value}"
    return label or value or "TBD"


def _mention(user: Optional[UserExtra]) -> str:
    if user is None:
        return "someone"
    slack_id = (user.slackUserId or "").strip()
    if slack_id:
        return f"<@{slack_id}>"
    return (user.name or user.email or "someone").strip()


def caller_round_text(
    interview: Interview,
    entry: Any,
    *,
    account: Optional[Account],
    owner: Optional[UserExtra],
    coworkers: list[UserExtra],
) -> str:
    """The #caller main post for one round; marked canceled once called off."""
    caller = entry.caller
    off = not caller.enabled or interview_status_value(getattr(entry, "status", None)) in ENDED_STATUSES
    when = slack_time(getattr(entry, "scheduledAt", None))
    company = (interview.companyName or "").strip() or "—"
    job_url = (interview.jobUrl or "").strip()
    recruiter = (getattr(entry, "interviewerName", None) or "").strip() or "—"
    flag = _country_flag(account.country if account else None)
    if flag and recruiter != "—":
        recruiter = f"{recruiter} {flag}"
    lines = [
        (f":x: *CANCELED* — ~Caller needed~ — posted by {_mention(owner)}" if off
         else f":telephone_receiver: *Caller needed* — posted by {_mention(owner)}"),
        f"*Profile:* {_profile_label(account) or '—'}",
        f"*Company name:* {company}",
        f"*JD Link:* {job_url or '—'}",
        f"*Recruiter name:* {recruiter}",
        f"*Stage:* {_stage_label(entry.stage)}",
        f"*Interview time:* {f'~{when}~' if off else when}",
        f"*Method:* {_method_text(caller)}",
        f"*Caller assigned:* {(getattr(caller, 'callerName', '') or '').strip() or 'TBD'}",
    ]
    if coworkers:
        lines.append(f"*Coworkers:* {' '.join(_mention(u) for u in coworkers)}")
    return "\n".join(lines)


def _snapshot_label(key: str, value: Any, mentions: dict[str, str]) -> str:
    if key == "stage":
        return _stage_label(value)
    if key == "status":
        return str(value).capitalize()
    if key == "method":
        return _METHOD_LABELS.get(value or "", value or "TBD")
    if key == "coworkers":
        return " ".join(mentions.get(str(i), "someone") for i in value) or "—"
    return str(value)


# Slack errors meaning the stored post is gone, so we post a fresh one.
_MESSAGE_GONE = {"message_not_found", "channel_not_found", "cant_update_message"}


async def _main_post_text(interview: Interview, entry: Any, owner: Optional[UserExtra]) -> str:
    caller = entry.caller
    account = await Account.get(interview.accountId) if interview.accountId else None
    coworkers = (
        await UserExtra.find({"_id": {"$in": list(caller.coworkerIds)}}).to_list()
        if caller.coworkerIds else []
    )
    return caller_round_text(interview, entry, account=account, owner=owner, coworkers=coworkers)


async def _change_lines(prev: Optional[dict], entry: Any) -> list[str]:
    if prev is None:
        return []
    new = round_caller_snapshot(entry) or {}
    ids = {str(i) for i in (prev.get("coworkers") or ())} | {str(i) for i in new.get("coworkers") or ()}
    users = await UserExtra.find({"_id": {"$in": [PydanticObjectId(i) for i in ids]}}).to_list() if ids else []
    mentions = {str(u.id): _mention(u) for u in users}
    return caller_change_lines(
        prev, new, fmt_time=slack_time, label=lambda k, v: _snapshot_label(k, v, mentions),
    )


async def _thread_reply(interview: Interview, entry: Any, kind: str, owner, prev: Optional[dict]) -> Optional[str]:
    if kind == "canceled":
        return f":no_entry: *Canceled* — {_stage_label(entry.stage)} · {slack_time(entry.scheduledAt)}"
    lines = await _change_lines(prev, entry)
    if kind == "restored":
        return "\n".join([f":white_check_mark: *Back on* — by {_mention(owner)}", *lines])
    if not lines:
        return None
    return "\n".join([f":pencil2: *Caller details updated* by {_mention(owner)}", *lines])


async def notify_round_caller(
    interview: Interview,
    entry: Any,
    kind: str,
    *,
    prev: Optional[dict] = None,
) -> Optional[str]:
    """Bring a round's #caller post in line with `kind`. Never raises.

    created → new main post; updated / canceled / restored → main post edited
    in place plus a thread reply saying what changed; deleted → main post
    removed. A main post deleted by hand in Slack is posted again (not for
    canceled). Returns the live main post's ts, or None.
    """
    try:
        caller = getattr(entry, "caller", None)
        channel = (get_settings().SLACK_CALLER_CHANNEL_ID or "").strip()
        if caller is None or not channel or not slack_bot_configured():
            return None
        parent = (caller.slackChannelTs or "").strip() or None
        if kind == "deleted":
            if parent:
                await _slack_api("chat.delete", {"channel": channel, "ts": parent})
            return None
        owner = await UserExtra.get(interview.createdBy)
        text = await _main_post_text(interview, entry, owner)
        if parent and kind != "created":
            try:
                await _slack_api("chat.update", {"channel": channel, "ts": parent, "text": text})
            except RuntimeError as exc:
                if str(exc) not in _MESSAGE_GONE:
                    raise
                log.info("slack_bot caller post %s gone (%s)", parent, exc)
                parent = None
        if parent and kind != "created":
            reply = await _thread_reply(interview, entry, kind, owner, prev)
            if reply:
                await _slack_api("chat.postMessage", {
                    "channel": channel, "text": reply, "thread_ts": parent,
                    "unfurl_links": False, "unfurl_media": False,
                })
            return parent
        if kind == "canceled":
            return None
        res = await _slack_api("chat.postMessage", {
            "channel": channel, "text": text, "unfurl_links": False, "unfurl_media": False,
        })
        return res.get("ts") or None
    except Exception as exc:
        log.warning("slack_bot round caller post failed kind=%s: %s", kind, exc)
        return None


def _slack_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _plan_day_label(day: datetime, offset: int = 0, sep: str = "") -> str:
    """Matches the SPA's planDayLabel, e.g. `9/27(Sun)`, or `9/27 (Sun)` with `sep=" "`."""
    d = day + timedelta(days=offset)
    return f"{d.month}/{d.day}{sep}({d.strftime('%a')})"


def daily_plan_text(plan: Any, owner: Optional[UserExtra], done: Any = None) -> str:
    """The day's follow-up: bids Goal → Done, interviews held, goal lines and notes.

    `done` is the day's Done with interviews from interview records; it
    defaults to what the plan stores.
    """
    goal = plan.goal
    done = done if done is not None else plan.done
    lines = [
        f":spiral_note_pad: *Daily plan* — {_mention(owner)}",
        f"*{_plan_day_label(plan.date, sep=' ')}*",
        f"Bids: self {done.bidsSelf}/{goal.bidsSelf} · bidder {done.bidsBidder}/{goal.bidsBidder}",
        f"Interviews: self {done.interviewsSelf} · caller {done.interviewsCaller}",
    ]
    for item in plan.goalItems or []:
        lines.append(f"{'✓' if item.done else '○'} {_slack_escape(item.text)}")
    notes = (plan.notes or "").strip()
    if notes:
        lines.append(_slack_escape(notes))
    monday = (plan.date - timedelta(days=plan.date.weekday())).date().isoformat()
    lines.append(f"<{_frontend_link(f'/report?week={monday}')}|Open Report>")
    return "\n".join(lines)


async def sync_daily_plan_message(
    plan: Any, owner: Optional[UserExtra], done: Any = None
) -> Optional[tuple[str, str]]:
    """Post the plan to SLACK_DAILY_PLAN_CHANNEL_ID, or edit the existing post
    in place. Never raises. Returns the live post's (channel, ts), or None when
    Slack is off or the call failed.
    """
    try:
        if not slack_bot_configured():
            return None
        text = daily_plan_text(plan, owner, done=done)
        channel = (plan.slackChannel or "").strip()
        ts = (plan.slackTs or "").strip()
        if channel and ts:
            try:
                await _slack_api("chat.update", {"channel": channel, "ts": ts, "text": text})
                return channel, ts
            except RuntimeError as exc:
                if str(exc) not in _MESSAGE_GONE:
                    raise
                log.info("slack_bot daily plan post %s gone (%s); reposting", ts, exc)
        target = (get_settings().SLACK_DAILY_PLAN_CHANNEL_ID or "").strip()
        if not target:
            return None
        res = await _slack_api(
            "chat.postMessage",
            {"channel": target, "text": text, "unfurl_links": False, "unfurl_media": False},
        )
        new_ts = res.get("ts")
        if not new_ts:
            return None
        return res.get("channel") or target, new_ts
    except Exception as exc:
        log.warning("slack_bot daily plan sync failed for plan %s: %s", plan.id, exc)
        return None


async def delete_daily_plan_message(plan: Any) -> None:
    """Remove the plan's Slack post, if any. Never raises."""
    channel = (plan.slackChannel or "").strip()
    ts = (plan.slackTs or "").strip()
    if not channel or not ts or not slack_bot_configured():
        return
    try:
        await _slack_api("chat.delete", {"channel": channel, "ts": ts})
    except Exception as exc:
        log.warning("slack_bot daily plan delete failed for plan %s: %s", plan.id, exc)


def _caller_start(iv: Interview) -> Optional[datetime]:
    caller = getattr(iv, "caller", None)
    if caller is None or not caller.enabled:
        return None
    return caller.startsAt


def _by_start(interviews: list[Interview]) -> list[Interview]:
    far = datetime.max.replace(tzinfo=timezone.utc)
    return sorted(interviews, key=lambda iv: (_caller_start(iv) is None, _caller_start(iv) or far))


def split_digest_interviews(
    interviews: list[Interview],
    user_id: Any,
) -> tuple[list[Interview], list[Interview]]:
    """(owned by user, joined as coworker). Owning wins when both apply."""
    mine: list[Interview] = []
    with_caller: list[Interview] = []
    seen: set[str] = set()
    for iv in interviews:
        key = str(iv.id)
        if key in seen:
            continue
        seen.add(key)
        if str(iv.createdBy) == str(user_id):
            mine.append(iv)
        else:
            with_caller.append(iv)
    return _by_start(mine), _by_start(with_caller)


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def digest_summary(mine: int, with_caller: int) -> str:
    total = mine + with_caller
    head = f"You have {_plural(total, 'interview')} today"
    if with_caller:
        head += f" ({mine} yours, {with_caller} as caller)"
    enjoy = "Enjoy your interview" if total == 1 else "Enjoy your interviews"
    return f"{head}. {enjoy}, and good luck! :four_leaf_clover:"


def _digest_line(
    n: int,
    iv: Interview,
    *,
    profile: str,
    tz: ZoneInfo,
    host: Optional[str] = None,
) -> str:
    starts = _caller_start(iv)
    parts: list[str] = []
    if starts is not None:
        parts.append(f"*{fmt_caller_time(starts, tz, with_day=False)}*")
    elif host is not None:
        parts.append("*Time TBD*")
    parts.append(profile.strip() or "—")
    company = (iv.companyName or "").strip() or "—"
    job_url = (iv.jobUrl or "").strip()
    parts.append(_mrkdwn_link(job_url, company) if job_url else company)
    parts.append(_stage_label(iv.stage))
    if host is not None:
        parts.append(f"host {host}")
        method = _method_text(iv.caller)
        if method != "TBD":
            parts.append(method)
    return f"{n}. " + " · ".join(parts)


def digest_blocks(
    mine: list[Interview],
    with_caller: list[Interview],
    *,
    profiles: dict[str, str],
    hosts: dict[str, str],
    tz: ZoneInfo,
    why: Optional[str] = None,
) -> list[dict[str, Any]]:
    total = len(mine) + len(with_caller)
    blocks: list[dict[str, Any]] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"Today's interviews ({total})", "emoji": True},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": digest_summary(len(mine), len(with_caller))},
        },
    ]
    n = 0

    def section(title: str, rows: list[Interview], *, as_caller: bool) -> None:
        nonlocal n
        lines = []
        for iv in rows:
            n += 1
            profile = profiles.get(str(iv.accountId), "") if iv.accountId else ""
            host = hosts.get(str(iv.createdBy), "someone") if as_caller else None
            lines.append(_digest_line(n, iv, profile=profile, tz=tz, host=host))
        blocks.append({"type": "divider"})
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*{title} ({len(rows)})*\n" + "\n".join(lines)},
            }
        )

    if mine:
        section("My interviews", mine, as_caller=False)
    if with_caller:
        section("Calls with you as caller", with_caller, as_caller=True)
    blocks.append(
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"<{_frontend_link('/interviews')}|Open interviews>"},
        }
    )
    if why:
        blocks.extend(
            [
                {"type": "divider"},
                {
                    "type": "context",
                    "elements": [{"type": "mrkdwn", "text": f"*Know your why*\n_{why}_"}],
                },
            ]
        )
    return blocks


async def run_digest_for_user(user: UserExtra) -> int:
    """Send daily digest if in slot and not yet sent today. Returns 1 if DM sent."""
    sent, reason = await _digest_for_user(user)
    if sent == 0 and reason:
        logger = log.debug if reason == "outside_slot" else log.info
        logger("slack_bot digest skip user=%s reason=%s", user.id, reason)
    return sent


async def _digest_for_user(user: UserExtra) -> tuple[int, Optional[str]]:
    slack_id = (user.slackUserId or "").strip()
    if not slack_id:
        return 0, "not_linked"
    s = get_settings()
    tz = resolve_user_tz(user)
    now_local = datetime.now(tz)
    hour = user.slackDigestHour if user.slackDigestHour is not None else s.SLACK_DIGEST_HOUR
    minute = user.slackDigestMinute if user.slackDigestMinute is not None else s.SLACK_DIGEST_MINUTE
    if not digest_slot_open(now_local, hour=hour, minute=minute):
        return 0, "outside_slot"
    day = now_local.date()
    day_key = day.isoformat()
    kind = f"digest:{day_key}"
    if await _already_sent(user.id, kind):
        return 0, "already_sent"

    start_n, end_n = calendar_day_bounds(day)
    interviews = await Interview.find(
        {
            "$or": [
                {"createdBy": user.id, "scheduledAt": {"$gte": start_n, "$lt": end_n}},
                # Callers live on rounds: match the coworker's round on this day.
                {"stageHistory": {"$elemMatch": {
                    "caller.enabled": True,
                    "caller.coworkerIds": user.id,
                    "scheduledAt": {"$gte": start_n, "$lt": end_n},
                }}},
            ],
        }
    ).sort([("scheduledAt", 1)]).to_list()
    interviews = [iv for iv in interviews if iv.status not in _SKIP_STATUSES]
    mine, with_caller = split_digest_interviews(interviews, user.id)
    if not mine and not with_caller:
        text = "You have no interviews today. Have a rest."
        blocks = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": "Today's interviews", "emoji": True},
            },
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": text},
            },
        ]
        ok = await send_dm(slack_id, text=text, blocks=blocks)
        if ok:
            await _mark_sent(user.id, kind)
            return 1, None
        return 0, "dm_failed"

    why = know_your_why_for_date(day)
    profiles = await _profile_labels(interviews)
    owner_ids = {iv.createdBy for iv in with_caller}
    owners = (
        await UserExtra.find({"_id": {"$in": list(owner_ids)}}).to_list() if owner_ids else []
    )
    hosts = {str(u.id): _mention(u) for u in owners}
    text = digest_summary(len(mine), len(with_caller))
    blocks = digest_blocks(
        mine,
        with_caller,
        profiles=profiles,
        hosts=hosts,
        tz=tz,
        why=why,
    )
    ok = await send_dm(slack_id, text=text, blocks=blocks)
    if ok:
        await _mark_sent(user.id, kind)
        return 1, None
    return 0, "dm_failed"


# ---------- #caller thread reminder, 30 minutes before ---------------------

CALLER_REMINDER_LEAD = timedelta(minutes=30)
# SlackAlertLog rows for channel posts (not tied to one user) use this id.
CALLER_CHANNEL_SENTINEL = PydanticObjectId("000000000000000000000000")


def _aware_utc(dt: datetime) -> datetime:
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


def due_caller_reminders(interviews: list, now: datetime) -> list[tuple[Any, Any]]:
    """(interview, round) for scheduled caller rounds starting in (now, now + 30 min], by start."""
    now = _aware_utc(now)
    rows = []
    for iv in interviews:
        for entry in getattr(iv, "stageHistory", None) or []:
            caller = getattr(entry, "caller", None)
            when = getattr(entry, "scheduledAt", None)
            if caller is None or not caller.enabled or when is None:
                continue
            if interview_status_value(entry.status) != "scheduled":
                continue
            if now < _aware_utc(when) <= now + CALLER_REMINDER_LEAD:
                rows.append((iv, entry))
    rows.sort(key=lambda row: _aware_utc(row[1].scheduledAt))
    return rows


def caller_reminder_key(entry: Any) -> str:
    """One reminder per round and start time, so a rescheduled round gets a new one."""
    return f"caller-reminder:{entry.id}:{int(_aware_utc(entry.scheduledAt).timestamp())}"


def caller_reminder_text(
    interview: Any,
    entry: Any,
    *,
    now: datetime,
    owner: Optional[UserExtra],
    coworkers: list[UserExtra],
) -> str:
    start = _aware_utc(entry.scheduledAt)
    minutes = max(1, math.ceil((start - _aware_utc(now)).total_seconds() / 60))
    company = (getattr(interview, "companyName", None) or "").strip() or "—"
    caller = entry.caller
    tags = list(dict.fromkeys([*(_mention(u) for u in coworkers), _mention(owner)]))
    return "\n".join([
        f":alarm_clock: *Starting in {minutes} min* — {slack_time(start, with_day=False)}"
        f" · {_stage_label(entry.stage)} · {company}",
        f"*Method:* {_method_text(caller)}",
        f"*Caller assigned:* {(caller.callerName or '').strip() or 'TBD'}",
        " ".join(tags),
    ])


async def _reminder_load(start: datetime, end: datetime) -> list:
    return await Interview.find(
        {"stageHistory": {"$elemMatch": {"caller.enabled": True, "scheduledAt": {"$gt": start, "$lte": end}}}}
    ).to_list()


async def _reminder_claim(key: str) -> bool:
    try:
        await SlackAlertLog(
            userId=CALLER_CHANNEL_SENTINEL, kind=key, sentAt=datetime.now(timezone.utc),
        ).insert()
        return True
    except Exception:  # duplicate (userId, kind) → already sent
        return False


async def _reminder_release(key: str) -> None:
    await SlackAlertLog.find(
        SlackAlertLog.userId == CALLER_CHANNEL_SENTINEL, SlackAlertLog.kind == key,
    ).delete()


async def _reminder_post(interview: Any, entry: Any, text: str) -> bool:
    """Reply in the round's #caller thread, or post to #caller when it has none."""
    channel = (get_settings().SLACK_CALLER_CHANNEL_ID or "").strip()
    body = {"channel": channel, "text": text, "unfurl_links": False, "unfurl_media": False}
    parent = (entry.caller.slackChannelTs or "").strip()
    try:
        try:
            await _slack_api("chat.postMessage", {**body, "thread_ts": parent} if parent else body)
        except RuntimeError as exc:
            if not parent or str(exc) not in _MESSAGE_GONE | {"thread_not_found"}:
                raise
            await _slack_api("chat.postMessage", body)
        return True
    except Exception as exc:
        log.warning("slack_bot caller reminder post failed for round %s: %s", entry.id, exc)
        return False


async def _reminder_people(interview: Any, entry: Any) -> tuple[Optional[UserExtra], list[UserExtra]]:
    ids = list(entry.caller.coworkerIds or [])
    coworkers = await UserExtra.find({"_id": {"$in": ids}}).to_list() if ids else []
    return await UserExtra.get(interview.createdBy), coworkers


def _reminder_configured() -> bool:
    return slack_bot_configured() and bool((get_settings().SLACK_CALLER_CHANNEL_ID or "").strip())


async def run_caller_reminders(
    now: datetime,
    *,
    load=_reminder_load,
    claim=_reminder_claim,
    release=_reminder_release,
    post=_reminder_post,
    people=_reminder_people,
    configured=_reminder_configured,
) -> dict[str, int]:
    """Remind each caller round's thread once, ~30 min before it starts (claim first, release on failure)."""
    sent = failed = 0
    if not configured():
        return {"sent": sent, "failed": failed}
    now = _aware_utc(now)
    window = (now.replace(tzinfo=None), (now + CALLER_REMINDER_LEAD).replace(tzinfo=None))
    for iv, entry in due_caller_reminders(await load(*window), now):
        key = caller_reminder_key(entry)
        if not await claim(key):
            continue
        ok = False
        try:
            owner, coworkers = await people(iv, entry)
            text = caller_reminder_text(iv, entry, now=now, owner=owner, coworkers=coworkers)
            ok = await post(iv, entry, text)
        except Exception as exc:
            log.warning("slack_bot caller reminder failed for round %s: %s", entry.id, exc)
        if ok:
            sent += 1
        else:
            failed += 1
            await release(key)
    return {"sent": sent, "failed": failed}


async def run_alert_tick() -> dict[str, int]:
    """Cron entrypoint: #caller thread reminders, then the daily digest for every linked user."""
    try:
        reminders = await run_caller_reminders(datetime.now(timezone.utc))
    except Exception as exc:  # never block the per-user digest
        log.exception("slack_bot caller reminders crashed: %s", exc)
        reminders = {"sent": 0, "failed": 0}
    caller_reminders = reminders["sent"]
    if not slack_bot_configured():
        return {"users": 0, "digest": 0, "skipped": 1, "caller_reminders": caller_reminders}
    users = await _linked_users()
    digest_total = 0
    skips = {
        "outside_slot": 0,
        "already_sent": 0,
        "no_interviews": 0,
        "dm_failed": 0,
        "not_linked": 0,
    }
    for user in users:
        try:
            sent, reason = await _digest_for_user(user)
            digest_total += sent
            if sent == 0 and reason:
                if reason in skips:
                    skips[reason] += 1
                logger = log.debug if reason == "outside_slot" else log.info
                logger("slack_bot digest skip user=%s reason=%s", user.id, reason)
        except Exception as exc:
            log.exception("slack_bot tick failed for user=%s: %s", user.id, exc)
    return {"users": len(users), "digest": digest_total, "skipped": 0, "caller_reminders": caller_reminders, **skips}
