"""Block Kit for the interview day post (a header, then one section per owner with one compact line per round and
Brief / Open links) and for the 30-minute reminders (`round_blocks`: one section + one actions block). Pure, no Slack
calls."""

from __future__ import annotations

from datetime import date
from typing import Any, Optional, Sequence

from app.interview_day.rounds import DayRound
from app.models import UserExtra
from app.slack_bot import _METHOD_LABELS, _mrkdwn_link, _stage_label, _text, slack_time

MAX_BLOCKS = 50  # Slack's limit per message
MAX_SECTION_TEXT = 3000  # Slack rejects the whole message when a section's text is longer
# Caps on typed text (the link field can hold a whole pasted invite), so one round cannot sink the post.
MAX_NAME, MAX_LINK = 200, 300
CONFIRM_ACTION_ID = "interview_confirm"  # no longer rendered (assigning confirms); old posts still carry it


def _who(user: Optional[UserExtra], fallback: str = "someone") -> str:
    """`<@slackUserId>` for an active, linked user; anyone else by plain name."""
    if user is None:
        return _short(fallback, MAX_NAME)
    slack_id = (getattr(user, "slackUserId", None) or "").strip()
    if slack_id and getattr(user, "isActive", True) is not False:
        return f"<@{slack_id}>"
    return _short(getattr(user, "name", None) or getattr(user, "email", None), MAX_NAME, _short(fallback, MAX_NAME))


def _short(value: Optional[str], limit: int, default: str = "") -> str:
    """Typed text for a Slack line: trimmed, cut to `limit` characters with an ellipsis, escaped."""
    raw = (value or "").strip()
    if len(raw) > limit:
        raw = raw[: limit - 1].rstrip() + "…"
    return _text(raw, default)


def _cap(text: str) -> str:
    """Last guard: at most 3000 characters, never ending inside a `<...>` token or an `&...;` entity."""
    if len(text) <= MAX_SECTION_TEXT:
        return text
    cut = text[: MAX_SECTION_TEXT - 1]
    for opener, closer in (("<", ">"), ("&", ";")):
        if cut.rfind(opener) > cut.rfind(closer):
            cut = cut[: cut.rfind(opener)]
    return cut + "…"


def _tags(users: Sequence[UserExtra]) -> str:
    return " ".join(dict.fromkeys(_who(u) for u in users))


def _method_part(r: DayRound) -> str:
    label = _METHOD_LABELS.get(r.method or "", "")
    link = (r.link or "").strip()
    linked = _mrkdwn_link(link, "link") if len(link) <= MAX_LINK else ""
    if link.startswith(("http://", "https://")) and linked.startswith("<"):
        link = linked
    else:  # a number, a note, or a URL Slack cannot link (spaces, <>|, too long): shown as text
        link = _short(link, MAX_LINK)
    return " ".join(p for p in (label, f"({link})" if link else "") if p)


def _on_the_call(r: DayRound, managers: Sequence[UserExtra]) -> str:
    owner = _who(r.owner, "—")
    flag = f" — {_tags(managers)}" if managers else ""
    if r.caller == "self":
        return f"On the call: Self · {owner}" if r.owner is not None else "On the call: Self"
    if r.caller == "tbd":
        return f"⚠️ No caller yet{flag} · owner {owner}"
    if r.caller == "user":
        name = _who(r.caller_user, r.caller_name)
        return f"On the call: {name} (caller) · owner {owner}"
    return f"caller {_short(r.caller_name, MAX_NAME, 'someone')} (external) · owner {owner}"


def _button(label: str, *, url: Optional[str] = None, style: Optional[str] = None, **extra: Any) -> dict:
    button: dict[str, Any] = {"type": "button", "text": {"type": "plain_text", "text": label, "emoji": True}}
    if url:
        button["url"] = url
    if style:
        button["style"] = style
    button.update(extra)
    return button


def round_blocks(
    r: DayRound, *, managers: Sequence[UserExtra], origin: str, starting_in: Optional[int] = None,
) -> list[dict]:
    """A round's section (mrkdwn) and its actions block. `starting_in` (minutes) adds the reminder header."""
    head = " · ".join(p for p in (slack_time(r.start, with_day=False), _short(r.company, MAX_NAME),
                                  _stage_label(r.stage), _short(r.profile, MAX_NAME)) if p)
    detail = " · ".join(p for p in (_short(r.position, MAX_NAME), f"Round {r.round_no}", _method_part(r)) if p)
    call = _on_the_call(r, managers)
    if r.thread_link and r.caller != "self":
        call += f" · {_mrkdwn_link(r.thread_link, 'thread')}"
    lines = [f"*{head}*", detail, call]
    if starting_in is not None:
        lines.insert(0, f"⏰ *Starting in {starting_in} min*")

    ref = f"{r.interview_id}:{r.round_id}"
    base = origin.rstrip("/")
    elements = [
        _button("Download brief", url=f"{base}/interview/{r.interview_id}?brief={r.round_id}"),
        _button("Open interview", url=f"{base}/interviews?panel={r.interview_id}"),
    ]
    if r.caller == "tbd":
        elements.append(_button("Assign caller", url=f"{base}/interviews/callers?round={ref}"))
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": _cap("\n".join(lines))}},
        {"type": "actions", "elements": elements},
    ]


def _title(day: date) -> str:
    return f"Interviews today — {day:%a} {day:%b} {day.day}"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def fallback_text(day: date, n: int) -> str:
    """Plain text for notifications and old clients."""
    return f"{_title(day)} · {_plural(n, 'interview')}"


_ENDED_MARKS = {"canceled": "❌ Canceled", "rejected": "❌ Rejected"}


def _app_base(origin: str) -> str:
    """The app's URL for the Brief / Open links; "" when it is not a usable http(s) URL (the links are left out)."""
    base = (origin or "").strip().rstrip("/")
    return base if base.startswith(("http://", "https://")) else ""


def _call_part(r: DayRound) -> str:
    """Who is on the call: Self, the caller (a platform user is tagged), or no caller yet."""
    if r.caller == "self":
        return "Self"
    if r.caller == "tbd":
        return "📞 No caller yet"
    if r.caller == "user":
        return f"📞 {_who(r.caller_user, r.caller_name)}"
    return f"📞 {_short(r.caller_name, MAX_NAME, 'someone')}"


def day_line(r: DayRound, base: str) -> str:
    """One round of the day post: `• time  Company · Stage · Interviewer — who's on the call · Brief · Open`. A
    called-off round is struck through and says so instead of naming the caller."""
    what = " · ".join(p for p in (_short(r.company, MAX_NAME, "—"), _stage_label(r.stage),
                                  _short(r.interviewer, MAX_NAME)) if p)
    head = f"{slack_time(r.start, with_day=False)}  {what}"
    ended = _ENDED_MARKS.get(r.status)
    parts = [f"~{head}~ — {ended}" if ended else f"{head} — {_call_part(r)}"]
    if base:
        parts.append(_mrkdwn_link(f"{base}/interview/{r.interview_id}?brief={r.round_id}", "Brief"))
        parts.append(_mrkdwn_link(f"{base}/interviews?panel={r.interview_id}", "Open"))
    return "• " + " · ".join(parts)


def _section(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": _cap(text)}}


def _owner_sections(owner: Optional[UserExtra], rounds: list[DayRound], base: str) -> list[dict]:
    """`@owner — N` and one line per round, in as many sections as Slack's 3000 characters need; a section after
    the first is headed `@owner (cont.)`."""
    who = _who(owner, "Unknown owner")
    sections: list[dict] = []
    current = [f"{who} — {len(rounds)}"]
    for r in rounds:
        line = day_line(r, base)
        if len(current) > 1 and len("\n".join([*current, line])) > MAX_SECTION_TEXT:
            sections.append(_section("\n".join(current)))
            current = [f"{who} (cont.)"]
        current.append(line)
    sections.append(_section("\n".join(current)))
    return sections


def _by_time(r: DayRound) -> tuple:
    return (r.start is None, r.start.timestamp() if r.start else 0.0)


def day_messages(
    day: date, rounds: list[DayRound], *, managers: Sequence[UserExtra] = (), origin: str,
) -> list[list[dict]]:
    """The day's post as messages of at most 50 blocks: a header with the counts, then one section per owner
    (owners ordered by their first round's time, then name; rounds by time). Only the first message has the
    header. `managers` is no longer used (kept for callers)."""
    if not rounds:
        return [[_section("No interviews today.")]]
    with_caller = sum(1 for r in rounds if r.caller in ("user", "external") and r.status not in _ENDED_MARKS)
    title = f"⏰ {_title(day)} · {_plural(len(rounds), 'interview')} · {with_caller} with a caller"
    header = {"type": "header", "text": {"type": "plain_text", "text": title, "emoji": True}}

    groups: dict[str, list[DayRound]] = {}
    for r in sorted(rounds, key=_by_time):
        groups.setdefault(r.owner_id or f"?{id(r.owner)}", []).append(r)

    def owner_name(rs: list[DayRound]) -> str:
        o = rs[0].owner
        return (getattr(o, "name", None) or getattr(o, "email", None) or "").lower()

    base = _app_base(origin)
    messages: list[list[dict]] = [[header]]
    for rs in sorted(groups.values(), key=lambda rs: (_by_time(rs[0]), owner_name(rs))):
        for block in _owner_sections(rs[0].owner, rs, base):
            if len(messages[-1]) >= MAX_BLOCKS:
                messages.append([])
            messages[-1].append(block)
    return messages
