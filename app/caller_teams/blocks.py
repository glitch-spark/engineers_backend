"""Block Kit for the team channel's "Interview scheduled" post and its thread replies. Pure, no Slack calls; no
buttons and no app links (the callers' workspace has no access to the app)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Optional
from zoneinfo import ZoneInfo

from app.interview_day.blocks import MAX_NAME, _method_part, _section, _short
from app.models.caller import Caller
from app.slack_bot import _stage_label, fmt_caller_time, slack_time

if TYPE_CHECKING:
    from app.caller_teams.sync import TeamRound

_HEAD = {
    "scheduled": ("📅 *Interview scheduled*", "Interview scheduled"),
    "canceled": ("~Interview scheduled~ — *Canceled*", "Canceled"),
    "off": ("~Interview scheduled~ — *Moved off this team*", "Moved off this team"),
}


def who(caller: Optional[Caller]) -> str:
    """`<@U…>` when the caller has a Slack ID, else the escaped name; "" for nobody."""
    if caller is None:
        return ""
    return f"<@{caller.slackUserId}>" if caller.slackUserId else _short(caller.name, MAX_NAME, "someone")


def when(tr: TeamRound) -> str:
    """Start in the team's timezone plus Slack's own-time tag, or "Time TBD"."""
    if tr.start is None:
        return "Time TBD"
    try:
        zone = ZoneInfo(tr.team.timezone)
    except Exception:  # noqa: BLE001 - a bad zone still shows Slack's own-time tag
        zone = ZoneInfo("America/Chicago")
    return f"{fmt_caller_time(tr.start, zone)} · {slack_time(tr.start)}"


def scheduled_blocks(tr: TeamRound, state: str) -> tuple[str, list[dict]]:
    """(fallback text, blocks) for the round's top-level post in `state` ("scheduled" | "canceled" | "off")."""
    head, plain = _HEAD[state]
    company = _short(tr.iv.companyName if tr.iv else "", MAX_NAME, "—")
    if tr.entry is None:
        return f"{plain}: {company}", [_section(f"{head}\n*{company}* — the round was removed.")]
    caller = tr.entry.caller
    lines = [
        head,
        f"*{company}* · {_stage_label(tr.entry.stage)}",
        f"When: {when(tr)}",
    ]
    method = _method_part(SimpleNamespace(method=caller.method if caller else None,
                                          link=caller.methodValue if caller else ""))
    if method:
        lines.append(f"Call: {method}")
    lines.append(f"Who: {who(tr.caller) or '—'}" + cc(tr))
    return f"{plain}: {company} · {_stage_label(tr.entry.stage)}", [_section("\n".join(lines))]


def cc(tr: TeamRound) -> str:
    """" · cc: <manager>", or "" when there is no manager or the manager is the caller."""
    if tr.manager is None or (tr.caller is not None and tr.caller.id == tr.manager.id):
        return ""
    return f" · cc: {who(tr.manager)}"


def reply_text(kind: str, tr: TeamRound, *, previous: Optional[Caller] = None) -> str:
    """A thread reply that tags whoever must act: the caller (or `previous` one) and the manager."""
    tags = " ".join(dict.fromkeys(p for p in (who(previous or tr.caller), who(tr.manager)) if p))
    if kind == "assigned":
        return f"Now on this call: {who(tr.caller)}" + cc(tr)
    if kind == "moved":
        if tr.start is None:
            return f"Time cleared — now TBD. {tags}"
        return f"Moved to {when(tr)}. {tags}"
    if kind == "time_set":
        return f"Time set: {when(tr)}. {tags}"
    if kind == "canceled":
        return f"Canceled. {tags}"
    if kind == "off":
        return f"Moved off this team. {tags}"
    if kind == "reinstated":
        return f"Scheduled again: {when(tr)}. {tags}"
    if kind == "back":
        return f"Back on this team: {tags}"
    raise ValueError(kind)
