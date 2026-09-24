"""Slack interview bot: OAuth identity link + private DMs.

Uses the workspace Bot Token for chat.postMessage DMs. Users connect via
Slack OAuth (user_scope=identity.basic) so we store their Slack member id
on UserExtra — never post interview details to a shared channel.

Sends an instant DM when an interview is created or meaningfully updated,
plus a daily digest at each user's chosen time/timezone. Interviews are
date-only, so there is no T-30 reminder.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import httpx
from beanie import PydanticObjectId

from app.config import get_settings
from app.know_your_why import know_your_why_for_date
from app.models import Account, Interview, UserExtra
from app.models.interview import InterviewStatus
from app.models.slack_alert_log import SlackAlertLog
from app.slack_timezones import normalize_slack_timezone, validate_timezone, zoneinfo_for_slack

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
    InterviewStatus.canceled,
    InterviewStatus.completed,
    InterviewStatus.passed,
    InterviewStatus.failed,
    InterviewStatus.no_show,
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


def _naive_dt(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.replace(tzinfo=None)
    return value


def interview_update_is_noteworthy(
    *,
    prev_scheduled_at: Optional[datetime],
    new_scheduled_at: Optional[datetime],
    prev_stage: Any,
    new_stage: Any,
    prev_status: Any,
    new_status: Any,
    prev_company: Optional[str],
    new_company: Optional[str],
) -> bool:
    """True when schedule, stage, status, or company changed (not note/transcript)."""
    if _naive_dt(prev_scheduled_at) != _naive_dt(new_scheduled_at):
        return True
    if _enum_key(prev_stage) != _enum_key(new_stage):
        return True
    if _enum_key(prev_status) != _enum_key(new_status):
        return True
    if (prev_company or "").strip() != (new_company or "").strip():
        return True
    return False


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
            "createdBy": user.id,
            "scheduledAt": {"$gte": start_n, "$lt": end_n},
        }
    ).sort([("scheduledAt", 1)]).to_list()
    interviews = [iv for iv in interviews if iv.status not in _SKIP_STATUSES]
    if not interviews:
        return 0, "no_interviews"

    why = know_your_why_for_date(day)
    title = f"Today's interviews ({len(interviews)})"
    profiles = await _profile_labels(interviews)
    blocks = _interview_blocks(
        title,
        interviews,
        why=why,
        profiles=profiles,
        tz=tz,
        include_open_link=True,
    )
    text = f"You have {len(interviews)} interview(s) today. Know your why: {why}"
    ok = await send_dm(slack_id, text=text, blocks=blocks)
    if ok:
        await _mark_sent(user.id, kind)
        return 1, None
    return 0, "dm_failed"


async def run_alert_tick() -> dict[str, int]:
    """Cron entrypoint: daily digest for every linked user."""
    if not slack_bot_configured():
        return {"users": 0, "digest": 0, "skipped": 1}
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
    return {"users": len(users), "digest": digest_total, "skipped": 0, **skips}
