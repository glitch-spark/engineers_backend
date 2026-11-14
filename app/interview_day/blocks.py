"""Block Kit for the interview day post: one section + one actions block per round. Pure, no Slack calls."""

from __future__ import annotations

from datetime import date
from typing import Any, Optional, Sequence

from app.interview_day.rounds import DayRound
from app.models import UserExtra
from app.slack_bot import _METHOD_LABELS, _mrkdwn_link, _slack_escape, _stage_label, _text, slack_time

MAX_BLOCKS = 50  # Slack's limit per message
CONFIRM_ACTION_ID = "interview_confirm"


def _who(user: Optional[UserExtra], fallback: str = "someone") -> str:
    """`<@slackUserId>` for an active, linked user; anyone else by plain name."""
    if user is None:
        return _text(fallback)
    slack_id = (getattr(user, "slackUserId", None) or "").strip()
    if slack_id and getattr(user, "isActive", True) is not False:
        return f"<@{slack_id}>"
    return _text(getattr(user, "name", None) or getattr(user, "email", None), _text(fallback))


def _tags(users: Sequence[UserExtra]) -> str:
    return " ".join(dict.fromkeys(_who(u) for u in users))


def _method_part(r: DayRound) -> str:
    label = _METHOD_LABELS.get(r.method or "", "")
    link = (r.link or "").strip()
    if link.startswith(("http://", "https://")):
        link = _mrkdwn_link(link, "link")
    else:
        link = _slack_escape(link)
    return " ".join(p for p in (label, f"({link})" if link else "") if p)


def _on_the_call(r: DayRound, managers: Sequence[UserExtra]) -> str:
    owner = _who(r.owner, "—")
    flag = f" — {_tags(managers)}" if managers else ""
    state = "✅ confirmed" if r.confirmed else "⏳ not confirmed"
    if r.caller == "self":
        return f"On the call: Self · {owner}" if r.owner is not None else "On the call: Self"
    if r.caller == "tbd":
        return f"⚠️ No caller yet{flag} · owner {owner}"
    if r.caller == "user":
        name = _who(r.caller_user, r.caller_name)
        return f"On the call: {name} (caller) {state} · owner {owner}"
    return f"caller {_text(r.caller_name)} (external) {state}{'' if r.confirmed else flag} · owner {owner}"


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
    head = " · ".join(p for p in (slack_time(r.start, with_day=False), _text(r.company, ""),
                                  _stage_label(r.stage), _text(r.profile, "")) if p)
    detail = " · ".join(p for p in (_text(r.position, ""), f"Round {r.round_no}", _method_part(r)) if p)
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
    elif r.caller in ("user", "external") and not r.confirmed:
        elements.append(_button("Confirm", style="primary", action_id=CONFIRM_ACTION_ID, value=ref))
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}},
        {"type": "actions", "elements": elements},
    ]


def _title(day: date) -> str:
    return f"Interviews today — {day:%a} {day:%b} {day.day}"


def fallback_text(day: date, n: int) -> str:
    """Plain text for notifications and old clients."""
    return f"{_title(day)} · {n} round{'' if n == 1 else 's'}"


def day_messages(
    day: date, rounds: list[DayRound], *, managers: Sequence[UserExtra], origin: str,
) -> list[list[dict]]:
    """The day's header and rounds as messages of at most 50 blocks. Only the first message has the header, and
    a round's section never ends up apart from its actions block."""
    if not rounds:
        return [[{"type": "section", "text": {"type": "mrkdwn", "text": "No interviews today."}}]]
    header = {"type": "header", "text": {"type": "plain_text", "text": f"⏰ {_title(day)}", "emoji": True}}
    messages: list[list[dict]] = [[header]]
    for r in rounds:
        unit = round_blocks(r, managers=managers, origin=origin)
        current = messages[-1]
        divider = [{"type": "divider"}] if current and current[-1]["type"] != "header" else []
        if len(current) + len(divider) + len(unit) > MAX_BLOCKS:
            current, divider = [], []
            messages.append(current)
        current.extend([*divider, *unit])
    return messages
