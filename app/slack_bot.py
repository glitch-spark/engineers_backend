"""Slack interview bot: #caller threads, reminders and the daily caller list.

Uses the workspace Bot Token (chat.postMessage). Users type their Slack member id in Profile; it is
stored on UserExtra and used to @-mention them (and for the Profile test DM). Channel posts: one "Caller needed" thread per
interview in the private SLACK_CALLER_CHANNEL_ID, the daily caller list in SLACK_CALLER_DAILY_CHANNEL_ID, and
each daily plan in SLACK_DAILY_PLAN_CHANNEL_ID (edited/deleted in place as the plan changes).

A round with a caller gets a reply in its #caller thread ~30 minutes before it starts, tagging the owner and
the interview managers. Each morning the day's caller interviews are listed in SLACK_CALLER_DAILY_CHANNEL_ID,
tagging each owner and cc'ing the managers; there are no per-user digest DMs. Transactions live in
app/slack_transactions.py.
"""

from __future__ import annotations

import logging
import re
import math
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterable, Optional, Sequence
from zoneinfo import ZoneInfo

import httpx
from beanie import PydanticObjectId

from app.interview_caller import (
    ENDED_STATUSES,
    caller_change_lines,
    caller_is_on,
    caller_thread_event,
    round_caller_snapshot,
)
from app.interview_normalize import interview_status_value

from app.config import get_settings
from app.models import Account, Interview, UserExtra
from app.models.interview import SlackThread
from app.models.slack_alert_log import SlackAlertLog
from app.slack_timezones import (
    format_gmt_offset,
    normalize_slack_timezone,
    validate_timezone,
    zoneinfo_for_slack,
)

log = logging.getLogger(__name__)

_SLACK_API = "https://slack.com/api"
# A Slack member ID as shown under Profile → ⋮ → Copy member ID.
_SLACK_MEMBER_ID = re.compile(r"[UW][A-Z0-9]{8,11}")


def slack_bot_configured() -> bool:
    return bool((get_settings().SLACK_BOT_TOKEN or "").strip())


def normalize_slack_id(raw: Optional[str]) -> Optional[str]:
    """A typed Slack member ID, trimmed and upper-cased; None for blank. Raises ValueError when malformed."""
    value = (raw or "").strip().upper()
    if not value:
        return None
    if not _SLACK_MEMBER_ID.fullmatch(value):
        raise ValueError("Enter your Slack member ID, like U04ABC12345 (Slack profile → ⋮ → Copy member ID)")
    return value


def resolve_user_tz(user: Optional[UserExtra] = None) -> ZoneInfo:
    """The user's IANA timezone (from the fixed GMT-offset allowlist), for their local day."""
    if user is not None and (user.slackTimezone or "").strip():
        return zoneinfo_for_slack(user.slackTimezone)
    app = (get_settings().APP_TIMEZONE or "").strip()
    return zoneinfo_for_slack(app or None)


def _frontend_link(path: str) -> str:
    return f"{get_settings().FRONTEND_ORIGIN.rstrip('/')}{path}"


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


def _stage_label(stage: Any) -> str:
    key = _enum_key(stage)
    if not key:
        return "—"
    if key == "ai_interview":
        return "AI Interview"
    return " ".join(part.capitalize() for part in key.split("_"))


# Characters that would end a `<url|label>` early or start another Slack token.
_UNLINKABLE = re.compile(r"[<>|\s]")


def _mrkdwn_link(url: Optional[str], label: str) -> str:
    """`<url|label>` for a well-formed http(s) URL, else just the escaped label. The label is escaped, the URL
    is left as is; a URL holding `<`, `>`, `|` or whitespace is never linked, so typed text cannot inject a token."""
    href = (url or "").strip()
    safe = _slack_escape((label or "").strip()).replace("|", " ").strip() or "link"
    if href.startswith(("http://", "https://")) and not _UNLINKABLE.search(href):
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


def _enum_key(value: Any) -> Optional[str]:
    if value is None:
        return None
    raw = getattr(value, "value", value)
    text = str(raw).strip()
    return text or None


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
    else:
        value = _slack_escape(value)
    if label and value:
        return f"{label} — {value}"
    return label or value or "TBD"


def _mention(user: Optional[UserExtra]) -> str:
    if user is None:
        return "someone"
    slack_id = (user.slackUserId or "").strip()
    if slack_id:
        return f"<@{slack_id}>"
    return _slack_escape((user.name or user.email or "someone").strip())


def cc_line(*users: Optional[UserExtra]) -> Optional[str]:
    """"cc @a @b": each user once, in order; None when there is nobody to cc."""
    mentions = dict.fromkeys(_mention(u) for u in users if u is not None)
    return f"cc {' '.join(mentions)}" if mentions else None


def caller_round_text(
    interview: Interview,
    entry: Any,
    *,
    account: Optional[Account],
    owner: Optional[UserExtra],
    managers: Sequence[UserExtra] = (),
) -> str:
    """The interview's #caller main post, describing its latest round: marked canceled once called off,
    "No caller needed" when that round has no caller. Ends with a cc line for the interview managers."""
    caller = entry.caller
    who = _mention(owner)
    when = slack_time(getattr(entry, "scheduledAt", None))
    company = _text(interview.companyName)
    profile = _text(_profile_label(account))
    if caller is None or not caller.enabled:
        lines = [
            f":telephone_receiver: {who} — *No caller needed* for {_stage_label(entry.stage)}",
            f"*Profile:* {profile}",
            f"*Company name:* {company}",
            f"*Stage:* {_stage_label(entry.stage)}",
            f"*Interview time:* {when}",
        ]
    else:
        off = interview_status_value(getattr(entry, "status", None)) in ENDED_STATUSES
        recruiter = _text(getattr(entry, "interviewerName", None))
        flag = _country_flag(account.country if account else None)
        if flag and recruiter != "—":
            recruiter = f"{recruiter} {flag}"
        lines = [
            (f":x: {who} — *Canceled* ~Caller needed~" if off else f":telephone_receiver: {who} — *Caller needed*"),
            f"*Profile:* {profile}",
            f"*Company name:* {company}",
            f"*JD Link:* {_text(interview.jobUrl)}",
            f"*Recruiter name:* {recruiter}",
            f"*Stage:* {_stage_label(entry.stage)}",
            f"*Interview time:* {f'~{when}~' if off else when}",
            f"*Method:* {_method_text(caller)}",
            f"*Caller assigned:* {_text(getattr(caller, 'callerName', ''), 'TBD')}",
        ]
        if not off:
            lines.append(":white_check_mark: Confirmed" if getattr(entry, "confirmed", False)
                         else ":hourglass_flowing_sand: Not confirmed yet")
    cc = cc_line(*managers)
    return "\n".join([*lines, cc] if cc else lines)


def caller_thread_reply(
    event: str, entry: Any, prev: Optional[dict], *, owner: Optional[UserExtra], managers: Sequence[UserExtra],
) -> Optional[str]:
    """The thread reply for a caller_thread_event: the owner first, a cc line for the interview managers last.
    None for "details" when no listed field changed."""
    who = _mention(owner)
    stage, when = _stage_label(entry.stage), slack_time(getattr(entry, "scheduledAt", None))
    caller = getattr(entry, "caller", None)
    if event == "moved":
        tail = "needs confirmation" if caller is not None and caller.enabled else "no caller needed"
        lines = [f":arrow_right: {who} — *Moved to {stage}* — {when} · {tail}"]
    elif event == "removed":
        lines = [f":wastebasket: {who} — *Round removed* — {stage}"]
    elif event == "ended":
        status_label = str(interview_status_value(getattr(entry, "status", None)) or "canceled").capitalize()
        lines = [f":no_entry: {who} — *{status_label}* — {stage} · {when}"]
    elif event == "back_on":
        lines = [f":white_check_mark: {who} — *Back on* — {stage} · {when} · needs confirmation"]
    elif event == "rescheduled":
        old = slack_time((prev or {}).get("time"))
        if getattr(entry, "confirmed", False):  # rescheduled and confirmed in one save
            name = _text(getattr(caller, "callerName", ""), "TBD")
            tail = f"confirmed, caller {name}"
        else:
            tail = "needs confirmation"
        lines = [f":repeat: {who} — *Rescheduled* — {old} → {when} · {tail}"]
    elif event == "confirmed":
        name = _text(getattr(caller, "callerName", ""), "TBD")
        lines = [f":white_check_mark: {who} — *Confirmed* — {stage} · {when} · caller {name}"]
    elif event == "unconfirmed":
        lines = [f":warning: {who} — *Confirmation removed* — {stage} · {when}"]
    elif event == "details":
        changes = _change_lines(prev, entry)
        if not changes:
            return None
        lines = [f":pencil2: {who} — *Caller details updated*", *changes]
    else:
        return None
    cc = cc_line(*managers)
    return "\n".join([*lines, cc] if cc else lines)


def _snapshot_label(key: str, value: Any) -> str:
    if key == "stage":
        return _stage_label(value)
    if key == "status":
        return str(value).capitalize()
    if key == "method":
        return _METHOD_LABELS.get(value or "", _slack_escape(value or "TBD"))
    return _slack_escape(str(value))


# Slack errors meaning the stored post is gone, so we post a fresh one.
_MESSAGE_GONE = {"message_not_found", "channel_not_found", "cant_update_message"}


async def _interview_account(interview: Any) -> Optional[Account]:
    return await Account.get(interview.accountId) if interview.accountId else None


async def _main_post_text(
    interview: Interview, entry: Any, owner: Optional[UserExtra], managers: Sequence[UserExtra] = (),
) -> str:
    return caller_round_text(interview, entry, account=await _interview_account(interview), owner=owner,
                             managers=managers)


async def interview_managers() -> list[UserExtra]:
    """Active users with the interview manager role, by name: tagged on every caller interview."""
    found = await UserExtra.find({"role": "interview_manager", "isActive": {"$ne": False}}).to_list()
    return sorted(found, key=lambda u: (u.name or u.email or "").lower())


async def _owner_and_managers(interview: Any) -> tuple[Optional[UserExtra], list[UserExtra]]:
    """The interview's owner and every interview manager."""
    return await UserExtra.get(interview.createdBy), await interview_managers()


async def _post_main(channel: str, text: str) -> Optional[str]:
    res = await _slack_api("chat.postMessage", {
        "channel": channel, "text": text, "unfurl_links": False, "unfurl_media": False,
    })
    return res.get("ts") or None


async def sync_caller_thread(
    interview: Any,
    before: dict[str, Optional[dict]],
    *,
    added: Iterable[str] = (),
    deleted: Iterable[str] = (),
) -> bool:
    """Keep the interview's single #caller thread in step with a write. Never raises.

    `before` holds each round's caller snapshot from before the write; `added` the rounds just appended;
    `deleted` the rounds being removed (all of them when the interview is deleted, which removes the
    main post and any older per-round posts). The first round with a caller on creates the thread; an
    interview that still has per-round posts adopts its latest one. Afterwards the main post is edited to
    describe the latest round and one thread reply says what changed. Returns True when
    `interview.slackThread` changed (the caller saves it).
    """
    try:
        channel = (get_settings().SLACK_CALLER_CHANNEL_ID or "").strip()
        if not channel or not slack_bot_configured():
            return False
        added, deleted = set(added), set(deleted)
        rounds = [e for e in (interview.stageHistory or []) if e.id]
        thread = interview.slackThread
        if rounds and all(e.id in deleted for e in rounds):
            posts = {thread.ts: thread.channel} if thread else {}
            for e in rounds:
                ts = (getattr(e.caller, "slackChannelTs", None) or "").strip()
                if ts:
                    posts.setdefault(ts, channel)
            for ts, ch in posts.items():
                await _slack_api("chat.delete", {"channel": ch, "ts": ts})
            return False
        live = [e for e in rounds if e.id not in deleted]
        if not live:
            return False
        latest = live[-1]

        changed = False
        if thread is None:
            legacy = next((e for e in reversed(rounds) if (getattr(e.caller, "slackChannelTs", None) or "").strip()), None)
            if legacy is not None:
                thread = SlackThread(channel=channel, ts=legacy.caller.slackChannelTs.strip())
                interview.slackThread, changed = thread, True
            else:
                if not any(caller_is_on(round_caller_snapshot(e)) for e in live):
                    return False
                owner, managers = await _owner_and_managers(interview)
                ts = await _post_main(channel, await _main_post_text(interview, latest, owner, managers))
                if ts:
                    interview.slackThread = SlackThread(channel=channel, ts=ts)
                return bool(ts)

        events, touched = [], False
        for e in rounds:
            prev = before.get(e.id)
            new = None if e.id in deleted else round_caller_snapshot(e)
            if prev != new or e.id in added or e.id in deleted:
                touched = True
            event = caller_thread_event(prev, new, added=e.id in added, removed=e.id in deleted)
            if event:
                events.append((event, e, prev))
        if not touched:
            return changed

        owner, managers = await _owner_and_managers(interview)
        text = await _main_post_text(interview, latest, owner, managers)
        try:
            await _slack_api("chat.update", {"channel": thread.channel, "ts": thread.ts, "text": text})
        except RuntimeError as exc:
            if str(exc) not in _MESSAGE_GONE:
                raise
            log.info("slack_bot caller thread %s gone (%s)", thread.ts, exc)
            ts = await _post_main(thread.channel, text)
            if ts:
                interview.slackThread = SlackThread(channel=thread.channel, ts=ts)
            return bool(ts) or changed

        # One reply per write: a new round says "moved"; otherwise the latest round that changed.
        pick = next((ev for ev in events if ev[0] == "moved"), events[-1] if events else None)
        if pick is not None:
            event, entry, prev = pick
            reply = caller_thread_reply(event, entry, prev, owner=owner, managers=managers)
            if reply:
                await _slack_api("chat.postMessage", {
                    "channel": thread.channel, "text": reply, "thread_ts": thread.ts,
                    "unfurl_links": False, "unfurl_media": False,
                })
        return changed
    except Exception as exc:
        log.warning("slack_bot caller thread sync failed for %s: %s", getattr(interview, "id", "?"), exc)
        return False


def _change_lines(prev: Optional[dict], entry: Any) -> list[str]:
    if prev is None:
        return []
    new = round_caller_snapshot(entry) or {}
    return caller_change_lines(prev, new, fmt_time=slack_time, label=_snapshot_label)


def _slack_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _text(value: Optional[str], default: str = "—") -> str:
    """User-entered text for a Slack line: trimmed and escaped (so it cannot ping or link), `default` when blank."""
    return _slack_escape((value or "").strip()) or default


def _plan_day_label(day: datetime, offset: int = 0, sep: str = "") -> str:
    """Matches the SPA's planDayLabel, e.g. `9/27(Sun)`, or `9/27 (Sun)` with `sep=" "`."""
    d = day + timedelta(days=offset)
    return f"{d.month}/{d.day}{sep}({d.strftime('%a')})"


_REGION_LABELS = (("bidsSelfUs", "US"), ("bidsSelfEu", "EU"), ("bidsSelfLatam", "LATAM"))


def _self_bids_text(done: Any, goal: Any) -> str:
    """`self 140/150`, plus the US / EU / LATAM split once one is typed."""
    text = f"self {done.bidsSelf}/{goal.bidsSelf}"
    split = [
        f"{label} {getattr(done, f, 0)}/{getattr(goal, f, 0)}"
        for f, label in _REGION_LABELS
    ]
    if any(getattr(c, f, 0) for c in (done, goal) for f, _ in _REGION_LABELS):
        text += f" ({' · '.join(split)})"
    return text


def daily_plan_text(plan: Any, owner: Optional[UserExtra], done: Any = None) -> str:
    """The day's follow-up: bids Goal → Done, interviews held, goal lines and notes.

    `done` is the day's Done with interviews from interview records; it
    defaults to what the plan stores.
    """
    goal = plan.goal
    done = done if done is not None else plan.done
    lines = [
        f":spiral_note_pad: {_mention(owner)}'s *Daily Plan*",
        f"*{_plan_day_label(plan.date, sep=' ')}*",
        f"Bids: {_self_bids_text(done, goal)} · bidder {done.bidsBidder}/{goal.bidsBidder}",
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
    managers: Sequence[UserExtra],
    account: Optional[Account],
) -> str:
    start = _aware_utc(entry.scheduledAt)
    minutes = max(1, math.ceil((start - _aware_utc(now)).total_seconds() / 60))
    company = _text(getattr(interview, "companyName", None))
    caller = entry.caller
    lines = [
        f":alarm_clock: *Starting in {minutes} min* — {slack_time(start, with_day=False)}"
        f" · {_stage_label(entry.stage)} · {company}",
        f"*Profile:* {_text(_profile_label(account))}",
        f"*Method:* {_method_text(caller)}",
        f"*Caller assigned:* {_text(caller.callerName, 'TBD')}",
    ]
    cc = cc_line(owner, *managers)
    return "\n".join([*lines, cc] if cc else lines)


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
    """Reply in the interview's #caller thread (or the round's older post), else post to #caller."""
    channel = (get_settings().SLACK_CALLER_CHANNEL_ID or "").strip()
    thread = getattr(interview, "slackThread", None)
    if thread is not None:
        channel, parent = thread.channel, thread.ts
    else:
        parent = (entry.caller.slackChannelTs or "").strip()
    body = {"channel": channel, "text": text, "unfurl_links": False, "unfurl_media": False}
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


async def _reminder_people(
    interview: Any, entry: Any,
) -> tuple[Optional[UserExtra], list[UserExtra], Optional[Account]]:
    """The interview's owner, every interview manager, and its profile account."""
    owner, managers = await _owner_and_managers(interview)
    return owner, managers, await _interview_account(interview)


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
            owner, managers, account = await people(iv, entry)
            text = caller_reminder_text(iv, entry, now=now, owner=owner, managers=managers, account=account)
            ok = await post(iv, entry, text)
        except Exception as exc:
            log.warning("slack_bot caller reminder failed for round %s: %s", entry.id, exc)
        if ok:
            sent += 1
        else:
            failed += 1
            await release(key)
    return {"sent": sent, "failed": failed}


# --- daily list of caller interviews ---------------------------------------------------------------


def caller_daily_due(now: datetime, *, at: time, tz: ZoneInfo) -> Optional[date]:
    """The local day whose list is due: from `at` until that local day ends; None before `at`."""
    local = _aware_utc(now).astimezone(tz)
    return local.date() if local.time() >= at else None


def caller_daily_bounds(day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """UTC-aware [local midnight, next local midnight) of `day` (23 or 25 hours across DST)."""
    start = datetime.combine(day, time.min, tzinfo=tz)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def _day_title(day: date) -> str:
    return f"{day:%a} {day:%b} {day.day}"


def caller_daily_text(day: date, rows: list[dict[str, Any]], managers: Sequence[UserExtra]) -> str:
    """The list for `day`: one line per active caller round, by start time, flagging unconfirmed ones and
    tagging each round's owner, then one cc line for the interview managers."""
    title = f":alarm_clock: *Caller interviews today* — {_day_title(day)}"
    if not rows:
        return f"{title} · none today."
    lines = [title]
    for r in sorted(rows, key=lambda r: r["start"]):
        parts = [slack_time(r["start"], with_day=False), r["company"], r["stage"], r["profile"],
                 f"caller {r['callerName']}", r["method"],
                 None if r["confirmed"] else ":hourglass_flowing_sand: not confirmed"]
        line = "• " + " · ".join(p for p in parts if p)
        if r["mentions"]:
            line += f" — {r['mentions']}"
        if r["link"]:
            line += f" · <{r['link']}|thread>"
        lines.append(line)
    cc = cc_line(*managers)
    return "\n".join([*lines, cc] if cc else lines)


def _is_listed(entry: Any, start: datetime, end: datetime) -> bool:
    caller = getattr(entry, "caller", None)
    at = getattr(entry, "scheduledAt", None)
    return bool(
        caller is not None and caller.enabled and at is not None
        and interview_status_value(getattr(entry, "status", None)) not in ENDED_STATUSES
        and start <= _aware_utc(at) < end
    )


async def _caller_daily_load(start: datetime, end: datetime) -> list[tuple[Any, Any]]:
    """(interview, round) for active caller rounds starting in [start, end), confirmed or not."""
    found = await Interview.find({"stageHistory": {"$elemMatch": {
        "caller.enabled": True, "status": {"$nin": list(ENDED_STATUSES)},
        "scheduledAt": {"$gte": start.replace(tzinfo=None), "$lt": end.replace(tzinfo=None)},
    }}}).to_list()
    return [(iv, e) for iv in found for e in iv.stageHistory if _is_listed(e, start, end)]


async def _caller_daily_people(interview: Any, entry: Any) -> dict[str, Any]:
    return {
        "owner": await UserExtra.get(interview.createdBy),
        "profile": _profile_label(await _interview_account(interview)),
    }


async def _caller_daily_permalink(interview: Any, entry: Any) -> Optional[str]:
    thread = getattr(interview, "slackThread", None)
    channel, ts = (thread.channel, thread.ts) if thread else (
        (get_settings().SLACK_CALLER_CHANNEL_ID or "").strip(), (entry.caller.slackChannelTs or "").strip())
    if not channel or not ts:
        return None
    try:
        return (await _slack_api("chat.getPermalink", {"channel": channel, "message_ts": ts})).get("permalink")
    except Exception as exc:
        log.info("slack_bot caller daily permalink failed: %s", exc)
        return None


async def _caller_daily_post(channel: str, text: str) -> bool:
    try:
        await _slack_api("chat.postMessage", {"channel": channel, "text": text, "unfurl_links": False,
                                              "unfurl_media": False})
        return True
    except Exception as exc:
        log.warning("slack_bot caller daily post failed: %s", exc)
        return False


def _caller_daily_configured() -> Optional[tuple[str, time, ZoneInfo]]:
    """(channel, time, zone) when the daily list is on; None when off or misconfigured."""
    s = get_settings()
    channel = (s.SLACK_CALLER_DAILY_CHANNEL_ID or "").strip()
    if not channel or not slack_bot_configured():
        return None
    try:
        hh, mm = (s.SLACK_CALLER_DAILY_TIME or "05:00").split(":")
        return channel, time(int(hh), int(mm)), ZoneInfo(s.SLACK_CALLER_DAILY_TIMEZONE or "America/Chicago")
    except Exception as exc:
        log.warning("slack_bot caller daily misconfigured: %s", exc)
        return None


async def run_caller_daily(
    now: datetime,
    *,
    load=_caller_daily_load,
    claim=_reminder_claim,
    release=_reminder_release,
    post=_caller_daily_post,
    people=_caller_daily_people,
    managers=interview_managers,
    permalink=_caller_daily_permalink,
    configured=_caller_daily_configured,
) -> str:
    """Post the day's caller interviews once (claim first, release on failure).

    Returns "posted", "not_due", "already_sent", "off" or "failed"."""
    cfg = configured()
    if cfg is None:
        return "off"
    channel, at, tz = cfg
    day = caller_daily_due(now, at=at, tz=tz)
    if day is None:
        return "not_due"
    key = f"caller-daily:{day.isoformat()}"
    if not await claim(key):
        return "already_sent"
    ok = False
    try:
        start, end = caller_daily_bounds(day, tz)
        rows = []
        for iv, entry in await load(start, end):
            who = await people(iv, entry)
            rows.append({
                "start": _aware_utc(entry.scheduledAt),
                "company": _text(iv.companyName),
                "stage": _stage_label(entry.stage),
                "profile": _slack_escape(who["profile"]),
                "callerName": _text(entry.caller.callerName, "TBD"),
                "method": _METHOD_LABELS.get(entry.caller.method or "", ""),
                "mentions": _mention(who["owner"]),
                "link": await permalink(iv, entry),
                "confirmed": bool(getattr(entry, "confirmed", False)),
            })
        ok = await post(channel, caller_daily_text(day, rows, await managers()))
    except Exception as exc:
        log.warning("slack_bot caller daily list failed: %s", exc)
    if not ok:
        await release(key)
        return "failed"
    return "posted"


async def run_alert_tick() -> dict[str, Any]:
    """Cron entrypoint: #caller thread reminders, then the daily caller list. Sends no DMs."""
    try:
        reminders = await run_caller_reminders(datetime.now(timezone.utc))
    except Exception as exc:  # never block the daily list
        log.exception("slack_bot caller reminders crashed: %s", exc)
        reminders = {"sent": 0, "failed": 0}
    try:
        caller_daily = await run_caller_daily(datetime.now(timezone.utc))
    except Exception as exc:
        log.exception("slack_bot caller daily list crashed: %s", exc)
        caller_daily = "failed"
    return {"caller_reminders": reminders["sent"], "caller_daily": caller_daily}
