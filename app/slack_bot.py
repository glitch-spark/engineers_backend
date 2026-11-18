"""Slack interview bot: #caller threads, the interview day post and its 30-minute reminders.

Uses the workspace Bot Token (chat.postMessage). Users type their Slack member id in Profile; it is
stored on UserExtra and used to @-mention them (and for the Profile test DM). Channel posts: one short post per
interview in the private SLACK_CALLER_CHANNEL_ID (the coordination channel), the interview day post in
SLACK_CALLER_DAILY_CHANNEL_ID, and each daily plan in SLACK_DAILY_PLAN_CHANNEL_ID (edited/deleted in place as the
plan changes).

The #caller post is built in app/coordination/blocks.py: the Choose caller select while the round is open, with Brief
and Open as text links, and no buttons once it is settled. It describes the interview's latest round and is rewritten
with chat.update whenever the round changes; the thread gets no change-log replies.

Each morning (SLACK_CALLER_DAILY_TIME) one day post lists every round of the local day grouped by owner, one compact
line each with who is on the call and Brief / Open links (assigning a caller confirms the round; old Confirm buttons
still work); it is edited in place whenever an interview changes. About 30 minutes before a round starts it gets its
own top-level reminder in the daily channel, with Download brief / Open interview / Assign caller buttons; that
message is the round's home (app/interview_day/home.py) and its follow-ups reply in its thread.
Dedup rows (SlackAlertLog) use the keys "interview-day:<YYYY-MM-DD>" and "round-reminder:<roundId>:<startEpoch>";
there are no per-user digest DMs. Once SLACK_CALLER_ASKS_FROM is set, app/coordination/chase.py also replies in each
round's home thread (caller and self rounds) 15 minutes after it ends (key "after-call:<roundId>:<startEpoch>") and
7 days after a completed round with no result (key "result-reminder:<roundId>").
The day post and reminders live in app/interview_day/, button clicks in app/interview_day/actions.py, transactions in
app/slack_transactions.py.
"""

from __future__ import annotations

import logging
import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo

import httpx
from beanie import PydanticObjectId

from app.interview_caller import caller_is_on, round_caller_snapshot, round_post_state

from app.config import get_settings
from app.models import Account, Interview, UserExtra
from app.models.interview import InterviewStageEvent, SlackThread
from app.models.plan_common import Counts, ItemResult
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


def _mention(user: Optional[UserExtra]) -> str:
    if user is None:
        return "someone"
    slack_id = (user.slackUserId or "").strip()
    if slack_id:
        return f"<@{slack_id}>"
    return _slack_escape((user.name or user.email or "someone").strip())


# Slack errors meaning the stored post is gone, so we post a fresh one.
_MESSAGE_GONE = {"message_not_found", "channel_not_found", "cant_update_message"}


async def _main_post(interview: Interview, latest: InterviewStageEvent) -> tuple[str, list[dict]]:
    """`(fallback text, blocks)` of the interview's coordination post, describing its latest round."""
    from app.caller_directory import list_directory
    from app.coordination.blocks import post_blocks
    from app.interview_day.rounds import day_round

    owner = await UserExtra.get(interview.createdBy)
    account = await Account.get(interview.accountId) if interview.accountId else None
    caller = latest.caller
    caller_user = None
    if caller is not None and caller.enabled and caller.callerKind == "user" and caller.callerId:
        try:
            caller_user = await UserExtra.get(PydanticObjectId(str(caller.callerId)))
        except Exception:  # a malformed stored id is just "no platform user"
            caller_user = None
    rounds = interview.stageHistory or []
    no = next((i for i, e in enumerate(rounds, 1) if e.id == latest.id), len(rounds))
    r = day_round(interview, no, latest, owner=owner, account=account, caller_user=caller_user)
    return post_blocks(
        r, directory=await list_directory(), managers=await interview_managers(),
        origin=get_settings().FRONTEND_ORIGIN.rstrip("/"),
    )


async def interview_managers() -> list[UserExtra]:
    """Active users with the interview manager role, by name: tagged on every caller interview."""
    found = await UserExtra.find({"role": "interview_manager", "isActive": {"$ne": False}}).to_list()
    return sorted(found, key=lambda u: (u.name or u.email or "").lower())


async def _post_main(channel: str, text: str, blocks: list[dict]) -> Optional[str]:
    res = await _slack_api("chat.postMessage", {
        "channel": channel, "text": text, "blocks": blocks, "unfurl_links": False, "unfurl_media": False,
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
    interview that still has per-round posts adopts its latest one. Afterwards the main post is edited in place
    to describe the latest round; the thread gets no change-log replies. Returns True when
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
                text, blocks = await _main_post(interview, latest)
                ts = await _post_main(channel, text, blocks)
                if ts:
                    interview.slackThread = SlackThread(channel=channel, ts=ts)
                return bool(ts)

        touched = any(
            before.get(e.id) != (None if e.id in deleted else round_post_state(e))
            or e.id in added or e.id in deleted
            for e in rounds
        )
        if not touched:
            return changed

        text, blocks = await _main_post(interview, latest)
        try:
            await _slack_api("chat.update", {"channel": thread.channel, "ts": thread.ts, "text": text, "blocks": blocks})
        except RuntimeError as exc:
            if str(exc) not in _MESSAGE_GONE:
                raise
            log.info("slack_bot caller thread %s gone (%s)", thread.ts, exc)
            ts = await _post_main(thread.channel, text, blocks)
            if ts:
                interview.slackThread = SlackThread(channel=thread.channel, ts=ts)
            return bool(ts) or changed
        return changed
    except Exception as exc:
        log.warning("slack_bot caller thread sync failed for %s: %s", getattr(interview, "id", "?"), exc)
        return False


def _slack_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _text(value: Optional[str], default: str = "—") -> str:
    """User-entered text for a Slack line: trimmed and escaped (so it cannot ping or link), `default` when blank."""
    return _slack_escape((value or "").strip()) or default


_REGION_LABELS = (("bidsSelfUs", "US"), ("bidsSelfEu", "EU"), ("bidsSelfLatam", "LATAM"))


# Interview stages in pipeline order, labelled as on the Interviews board.
_ROUND_LABELS = {
    "intro": "Intro",
    "tech_round_1": "Tech round 1",
    "tech_round_2": "Tech round 2",
    "live_coding": "Live Coding",
    "system_design": "System Design",
    "cultural": "Hiring Manager",
    "panel": "Panel",
    "final": "Final",
}
_ROUND_ORDER = list(_ROUND_LABELS)

_PLAN_DIVIDER = "━" * 30
_PLAN_INDENT = " " * 3


def _plan_number(n: int) -> str:
    """Keycap emoji for 1–10 (they render inside a code block), `11.` after that."""
    if n == 10:
        return "🔟"
    return f"{n}️⃣" if n < 10 else f"{n}."


def _plan_block_text(value: Optional[str]) -> str:
    """User text inside the post's code block: escaped, and without backticks
    that could close the block early."""
    return _slack_escape((value or "").strip()).replace("`", "ˋ")


def _rounds_text(count: int, rounds: Optional[dict]) -> str:
    """`2   Intro(1) · Tech round 1(1)`: rounds held, then by stage in pipeline order."""
    by_stage = sorted(
        ((stage, n) for stage, n in (rounds or {}).items() if n),
        key=lambda s: (_ROUND_ORDER.index(s[0]) if s[0] in _ROUND_LABELS else len(_ROUND_ORDER), s[0]),
    )
    if not by_stage:
        return str(count)
    stages = " · ".join(f"{_ROUND_LABELS.get(stage) or _stage_label(stage)}({n})" for stage, n in by_stage)
    return f"{count}   {stages}"


def _n(x: Optional[float], hours: bool = False) -> str:
    """`2`, `1.5`; with `hours`, `2h`, `1.5h`."""
    x = x or 0
    s = str(int(x)) if float(x).is_integer() else f"{x:g}"
    return s + "h" if hours else s


def _mark(done: float, target: Optional[float]) -> str:
    return "" if not target else "  ✅" if done >= target else "  ⚠️"


def _plan_line(label: str, value: str) -> str:
    return f"{_PLAN_INDENT}{_plan_block_text(label):<16}  {value}"


def _bids_line(label: str, field: str, done: Any, targets: Any, regions: bool = False) -> str:
    d, t = getattr(done, field), getattr(targets, field)
    value = f"{d} / {t}{_mark(d, t)}" if t else str(d)
    if regions:
        parts = [f"{lab} {getattr(done, f, 0)}/{getattr(targets, f, 0)}" for f, lab in _REGION_LABELS
                 if getattr(done, f, 0) or getattr(targets, f, 0)]
        # Canada, Asia and no-region profiles (or a pre-split total) count in Self only: show them so the parts add up.
        other = d - sum(getattr(done, f, 0) for f, _ in _REGION_LABELS)
        if parts and other > 0:
            parts.append(f"Other {other}")
        if parts:
            value += f"  ({' · '.join(parts)})"
    return _plan_line(label, value)


def _item_line(e: dict, progress: dict) -> Optional[str]:
    item, result = e["item"], e["result"]
    hours = item.kind == "hours"
    if item.kind != "check" and result and result.done and result.value is None:
        return _plan_line(item.text, "✅" if e["scope"] == "routine" else "✅ done today")  # migrated, no number
    if e["scope"] == "routine":
        result = result or ItemResult()  # planned but not reported: shows as short
        if item.kind == "check":
            return _plan_line(item.text, "✅" if result.done else "❌ not done")
        v = result.value or 0
        t = result.target if result.target is not None else item.target
        return _plan_line(item.text, (f"{_n(v, hours)} / {_n(t, hours)}" if t else _n(v, hours)) + ("  ✅" if result.done else _mark(v, t)))
    # weekly goal: the week so far
    if item.kind == "check":
        return _plan_line(item.text, "✅ done today" if result and result.done else "in focus")
    p = progress.get(item.id) or {"value": 0, "target": item.target}
    text = f"{_n(p['value'], hours)} / {_n(p['target'], hours)}" if p["target"] else _n(p["value"], hours)
    return _plan_line(item.text, text + ("  ✅" if p.get("done") else ""))


def daily_plan_text(
    plan: Any, owner: Optional[UserExtra], *, done: Any = None, rounds: Optional[dict] = None,
    targets: Any = None, items: Optional[list] = None, progress: Optional[dict] = None,
    week_interviews: Optional[tuple] = None,
) -> str:
    """The day's Goal vs Done standup: bids, interviews, routines, weekly goals (those touched today), extra.

    The sections sit in a code block so the columns line up; the heading (with the owner's mention) and the
    report link stay outside it. `done` defaults to what the plan stores; `rounds` is {"self": {stage: n},
    "caller": {stage: n}}; `items` are `plan_auto.day_items` lines; `progress` is `goal_progress`;
    `week_interviews` is (held Monday..today, week target), the Week line is left out without a target.
    """
    targets = targets if targets is not None else Counts()
    done = done if done is not None else plan.done
    rounds = rounds or {}
    progress = progress or {}
    day = f"{plan.date.strftime('%a')} {plan.date.month}/{plan.date.day}"
    sections: list[list[str]] = []

    sections.append(["Bids", _bids_line("Self", "bidsSelf", done, targets, regions=True),
                     _bids_line("Bidder", "bidsBidder", done, targets)])

    interviews = [
        _plan_line("Self", _rounds_text(done.interviewsSelf, rounds.get("self"))),
        _plan_line("Caller", _rounds_text(done.interviewsCaller, rounds.get("caller"))),
    ]
    if week_interviews and week_interviews[1]:
        interviews.append(_plan_line("Week", f"{week_interviews[0]} / {week_interviews[1]}"))
    sections.append(["Interviews", *interviews])

    routines = [ln for e in items or [] if e["scope"] == "routine" and (ln := _item_line(e, progress))]
    if routines:
        sections.append(["Routines", *routines])
    touched = set(plan.focus or [])
    goals = [
        ln for e in items or []
        if e["scope"] == "goal"
        and (e["item"].id in touched or (e["result"] and (e["result"].done or e["result"].value)))
        and (ln := _item_line(e, progress))
    ]
    if goals:
        sections.append(["Weekly goals (week so far)", *goals])

    notes = [line.rstrip() for line in _plan_block_text(plan.notes).splitlines()]
    if notes:
        sections.append(["Extra", *(f"{_PLAN_INDENT}{line}" if line else "" for line in notes)])

    body: list[str] = [f"{_PLAN_INDENT}{' ' * 18}Done / Goal"]
    for n, (title, *rows) in enumerate(sections, start=1):
        body += [f"{_plan_number(n)} {title}", *rows]

    return "\n".join([
        f"🗒️ {_mention(owner)} · Daily standup · {day}",
        "```",
        _PLAN_DIVIDER,
        *body,
        _PLAN_DIVIDER,
        "```",
        f"🔗 <{_frontend_link(f'/plans?day={plan.date.date().isoformat()}')}|Open Daily plan>",
    ])


async def sync_daily_plan_message(plan: Any, owner: Optional[UserExtra], **view: Any) -> Optional[tuple[str, str]]:
    """Post the plan to SLACK_DAILY_PLAN_CHANNEL_ID, or edit the existing post
    in place. Never raises. Returns the live post's (channel, ts), or None when
    Slack is off or the call failed.
    """
    try:
        if not slack_bot_configured():
            return None
        text = daily_plan_text(plan, owner, **view)
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
    if not channel or not ts or not ts[0].isdigit() or not slack_bot_configured():  # not a first-post claim
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


# --- the daily post's schedule (the post itself is app/interview_day/post.py) -------------------------


def caller_daily_due(now: datetime, *, at: time, tz: ZoneInfo) -> Optional[date]:
    """The local day whose list is due: from `at` until that local day ends; None before `at`."""
    local = _aware_utc(now).astimezone(tz)
    return local.date() if local.time() >= at else None


def caller_daily_bounds(day: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    """UTC-aware [local midnight, next local midnight) of `day` (23 or 25 hours across DST)."""
    start = datetime.combine(day, time.min, tzinfo=tz)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=tz)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


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


async def run_alert_tick() -> dict[str, Any]:
    """Cron entrypoint: the day post first, then the 30-minute round reminders (each its round's own message), then a refresh of today's post so changes no interview
    write reports (renames, deactivations, Slack ID changes, archives, a refresh that failed) reach it. Each step
    is isolated from the others; the coordination asks (app/coordination/chase.py) come last. Sends no DMs."""
    from app.interview_day.post import refresh_today, run_day_post  # post imports this module
    from app.interview_day.reminders import run_round_reminders

    try:
        caller_daily = await run_day_post(datetime.now(timezone.utc))
    except Exception as exc:
        log.exception("slack_bot interview day post crashed: %s", exc)
        caller_daily = "failed"
    try:
        reminders = await run_round_reminders(datetime.now(timezone.utc))
    except Exception as exc:
        log.exception("slack_bot round reminders crashed: %s", exc)
        reminders = {"sent": 0, "failed": 0}
    try:
        await refresh_today()  # no interview id: the unconditional refresh
    except Exception as exc:
        log.warning("slack_bot day post refresh failed: %s", exc)
    try:
        from app.coordination.chase import run_coordination_chase  # imports the interview router, so not at the top

        chase = await run_coordination_chase(datetime.now(timezone.utc))
    except Exception as exc:
        log.exception("slack_bot coordination chase crashed: %s", exc)
        chase = {}
    try:
        from app.standup.nudge import run_standup_nudge

        standup_nudge = await run_standup_nudge(datetime.now(timezone.utc))
    except Exception as exc:
        log.exception("slack_bot standup nudge crashed: %s", exc)
        standup_nudge = "failed"
    return {
        "caller_reminders": reminders["sent"], "caller_daily": caller_daily, "standup_nudge": standup_nudge,
        "after_call_asks": chase.get("after_call_asks", 0), "result_reminders": chase.get("result_reminders", 0),
    }
