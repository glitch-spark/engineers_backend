"""Slack interview bot: OAuth identity link + private DM reminders.

Uses the workspace Bot Token for chat.postMessage DMs. Users connect via
Slack OAuth (user_scope=identity.basic) so we store their Slack member id
on UserExtra — never post interview details to a shared channel.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import httpx
from beanie import PydanticObjectId

from app.config import get_settings
from app.models import Interview, UserExtra
from app.models.interview import InterviewStatus
from app.models.slack_alert_log import SlackAlertLog

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


def _app_tz() -> ZoneInfo:
    name = (get_settings().APP_TIMEZONE or "").strip() or "UTC"
    try:
        return ZoneInfo(name)
    except Exception:
        log.warning("slack_bot: invalid APP_TIMEZONE=%r — UTC", name)
        return ZoneInfo("UTC")


def _now_app() -> datetime:
    return datetime.now(_app_tz())


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


def _fmt_when(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_app_tz())
    local = dt.astimezone(_app_tz())
    return local.strftime("%a %b %d · %I:%M %p %Z")


def _interview_blocks(title: str, interviews: list[Interview]) -> list[dict[str, Any]]:
    lines = []
    for iv in interviews:
        company = (iv.companyName or "Interview").strip()
        stage = iv.stage.value if iv.stage else "—"
        when = _fmt_when(iv.scheduledAt)
        pos = (iv.appliedPosition or "").strip()
        extra = f" · {pos}" if pos else ""
        lines.append(f"• *{company}*{extra}\n  {when} · `{stage}`")
    body = "\n\n".join(lines) if lines else "_No interviews._"
    return [
        {"type": "header", "text": {"type": "plain_text", "text": title, "emoji": True}},
        {"type": "section", "text": {"type": "mrkdwn", "text": body}},
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"<{_frontend_link('/interviews')}|Open interviews>",
            },
        },
    ]


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


def reminder_window(
    now: datetime,
    *,
    minutes: int,
    skew_seconds: int = 90,
) -> tuple[datetime, datetime]:
    """Return [target − skew, target + skew] for scheduledAt matching."""
    target = now + timedelta(minutes=minutes)
    skew = timedelta(seconds=skew_seconds)
    return target - skew, target + skew


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


def _as_naive_utc_bounds(start: datetime, end: datetime) -> tuple[datetime, datetime]:
    """Interview.scheduledAt is often naive; compare with naive UTC window."""
    def to_naive_utc(dt: datetime) -> datetime:
        if dt.tzinfo is None:
            return dt
        return dt.astimezone(timezone.utc).replace(tzinfo=None)

    return to_naive_utc(start), to_naive_utc(end)


async def run_t30_for_user(user: UserExtra, now: datetime) -> int:
    """Send T-30 DMs for this user. Returns number of DMs sent."""
    slack_id = (user.slackUserId or "").strip()
    if not slack_id:
        return 0
    minutes = get_settings().SLACK_REMINDER_MINUTES or 30
    start, end = reminder_window(now, minutes=minutes)
    start_n, end_n = _as_naive_utc_bounds(start, end)

    candidates = await Interview.find(
        {
            "createdBy": user.id,
            "scheduledAt": {"$gte": start_n, "$lte": end_n},
        }
    ).to_list()

    sent = 0
    for iv in candidates:
        if iv.status in _SKIP_STATUSES:
            continue
        kind = f"t30:{iv.id}"
        if await _already_sent(user.id, kind):
            continue
        title = "Interview in 30 minutes"
        blocks = _interview_blocks(title, [iv])
        text = f"Interview reminder: {iv.companyName or 'upcoming'} at {_fmt_when(iv.scheduledAt)}"
        ok = await send_dm(slack_id, text=text, blocks=blocks)
        if ok:
            await _mark_sent(user.id, kind, interview_id=iv.id)
            sent += 1
    return sent


async def run_digest_for_user(user: UserExtra, now_local: datetime) -> int:
    """Send morning digest if in slot and not yet sent today."""
    slack_id = (user.slackUserId or "").strip()
    if not slack_id:
        return 0
    s = get_settings()
    hour = user.slackDigestHour if user.slackDigestHour is not None else s.SLACK_DIGEST_HOUR
    minute = user.slackDigestMinute if user.slackDigestMinute is not None else s.SLACK_DIGEST_MINUTE
    if not digest_slot_open(now_local, hour=hour, minute=minute):
        return 0
    day_key = now_local.strftime("%Y-%m-%d")
    kind = f"digest:{day_key}"
    if await _already_sent(user.id, kind):
        return 0

    day_start = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    start_n = day_start.replace(tzinfo=None)
    end_n = day_end.replace(tzinfo=None)
    interviews = await Interview.find(
        {
            "createdBy": user.id,
            "scheduledAt": {"$gte": start_n, "$lt": end_n},
        }
    ).sort([("scheduledAt", 1)]).to_list()
    interviews = [iv for iv in interviews if iv.status not in _SKIP_STATUSES]
    if not interviews:
        await _mark_sent(user.id, kind)
        return 0

    title = f"Today's interviews ({len(interviews)})"
    blocks = _interview_blocks(title, interviews)
    text = f"You have {len(interviews)} interview(s) today."
    ok = await send_dm(slack_id, text=text, blocks=blocks)
    if ok:
        await _mark_sent(user.id, kind)
        return 1
    return 0


async def run_alert_tick() -> dict[str, int]:
    """Cron entrypoint: T-30 + digest for every linked user."""
    if not slack_bot_configured():
        return {"users": 0, "t30": 0, "digest": 0, "skipped": 1}
    users = await _linked_users()
    now_local = _now_app()
    now_utc = datetime.now(timezone.utc)
    t30_total = 0
    digest_total = 0
    for user in users:
        try:
            t30_total += await run_t30_for_user(user, now_utc)
            digest_total += await run_digest_for_user(user, now_local)
        except Exception as exc:
            log.exception("slack_bot tick failed for user=%s: %s", user.id, exc)
    return {"users": len(users), "t30": t30_total, "digest": digest_total, "skipped": 0}
