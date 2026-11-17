"""Block Kit for the coordination channel's short post: one section + one actions block. Pure, no Slack calls."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional, Sequence

from app.caller_directory import CallerEntry
from app.interview_day.blocks import MAX_NAME, _button, _cap, _method_part, _short, _tags, _who
from app.interview_day.rounds import DayRound
from app.models import UserExtra
from app.slack_bot import _stage_label, slack_time

COORD_CHOOSE_CALLER = "coord_choose_caller"  # static select
COORD_CONFIRM = "coord_confirm"
COORD_OPEN_BRIEF = "coord_open_brief"  # URL buttons: the server acknowledges them and does nothing
COORD_OPEN_INTERVIEW = "coord_open_interview"
COORD_AFTER_COMPLETED = "coord_after_completed"
COORD_AFTER_CANCELED = "coord_after_canceled"
COORD_AFTER_RESCHEDULED = "coord_after_rescheduled"
COORD_RESULT_PASSED = "coord_result_passed"
COORD_RESULT_REJECTED = "coord_result_rejected"

# Every id the server acts on (the two URL buttons are only acknowledged).
COORD_ACTIONS = frozenset({
    COORD_CHOOSE_CALLER, COORD_CONFIRM, COORD_AFTER_COMPLETED, COORD_AFTER_CANCELED, COORD_AFTER_RESCHEDULED,
    COORD_RESULT_PASSED, COORD_RESULT_REJECTED,
})

SELF = "self"  # the last part of the "No caller (Self)" option value
MAX_GROUP_OPTIONS = 100  # Slack's limit per option group
MAX_OPTION_TEXT = 75  # Slack's limit for an option's text


def _plain(text: str) -> dict:
    return {"type": "plain_text", "text": text}


def _option_text(name: Optional[str]) -> str:
    """An option's label: plain text, so it is cut to the limit but not escaped (an escape would show literally)."""
    raw = (name or "").strip() or "Unnamed"
    if len(raw) > MAX_OPTION_TEXT:
        raw = raw[: MAX_OPTION_TEXT - 1].rstrip() + "…"
    return raw


def _ref(r: DayRound) -> str:
    return f"{r.interview_id}:{r.round_id}"


def caller_select(r: DayRound, directory: Sequence[CallerEntry]) -> dict:
    """The Choose caller select. Its option values are `<interview>:<round>:<callerId|self>`; the option of the
    round's current caller is preselected, none when that caller is not listed (archived, deactivated, "tbd")."""
    ref = _ref(r)
    owner_id = str(getattr(r.owner, "id", "") or "")

    def option(label: str, caller_id: str) -> dict:
        return {"text": _plain(_option_text(label)), "value": f"{ref}:{caller_id}"}

    people = [option(e["name"], e["_id"]) for e in directory if e["kind"] == "user" and e["_id"] != owner_id]
    external = [option(e["name"], e["_id"]) for e in directory if e["kind"] == "external"]
    no_caller = [option("No caller (Self)", SELF)]
    groups = [(label, options[:MAX_GROUP_OPTIONS]) for label, options in
              (("People", people), ("External callers", external), ("No caller", no_caller)) if options]

    select: dict[str, Any] = {
        "type": "static_select",
        "action_id": COORD_CHOOSE_CALLER,
        "placeholder": _plain("Choose caller"),
        "option_groups": [{"label": _plain(label), "options": options} for label, options in groups],
    }
    current = f"{ref}:{SELF}" if r.caller == "self" else f"{ref}:{r.caller_id}" if r.caller_id else None
    chosen = next((o for _, options in groups for o in options if o["value"] == current), None)
    if chosen is not None:
        select["initial_option"] = chosen
    return select


def _caller_part(r: DayRound, managers: Sequence[UserExtra]) -> str:
    if r.caller == "self":
        return "No caller (Self)"
    if r.caller == "tbd":
        return f"⚠️ No caller yet — {_tags(managers)}" if managers else "⚠️ No caller yet"
    state = "✅ confirmed" if r.confirmed else "⏳ not confirmed"
    part = f"Caller: {_who(r.caller_user, r.caller_name)} {state}"
    method = _method_part(r)
    return f"{part} · {method}" if method else part


_STATUS_LINES = {
    "completed": "Completed · waiting for the result",
    "passed": "🎉 Passed",
    "rejected": "❌ Rejected",
    "canceled": "❌ Canceled",
}


def post_blocks(
    r: DayRound, *, directory: Sequence[CallerEntry], managers: Sequence[UserExtra], origin: str,
) -> tuple[str, list[dict]]:
    """`(fallback text, [section, actions])` of an interview's coordination post."""
    company = _short(r.company, MAX_NAME, "—")
    stage = _stage_label(r.stage)
    parts = [company, stage, slack_time(r.start), _short(r.profile, MAX_NAME)]
    head = " · ".join(p for p in parts if p)
    ended = r.status in ("canceled", "rejected")
    lines = [f"❌ ~{head}~" if ended else f"📞 *{head}*",
             f"Owner {_who(r.owner, '—')} · {_caller_part(r, managers)}"]
    if r.status in _STATUS_LINES:
        lines.append(_STATUS_LINES[r.status])

    ref = _ref(r)
    base = origin.rstrip("/")
    links = [
        _button("Download brief", url=f"{base}/interview/{r.interview_id}?brief={r.round_id}",
                action_id=COORD_OPEN_BRIEF),
        _button("Open interview", url=f"{base}/interviews?panel={r.interview_id}", action_id=COORD_OPEN_INTERVIEW),
    ]
    if r.status == "completed":
        elements = [
            _button("Passed", style="primary", action_id=COORD_RESULT_PASSED, value=ref),
            _button("Rejected", style="danger", action_id=COORD_RESULT_REJECTED, value=ref),
            *links,
        ]
    elif r.status in _STATUS_LINES:  # passed, rejected, canceled: nothing left to do but look
        elements = links
    else:
        elements = [caller_select(r, directory)]
        if r.caller in ("user", "external") and not r.confirmed:
            elements.append(_button("Confirm", style="primary", action_id=COORD_CONFIRM, value=ref))
        elements += links
    return f"📞 {company} · {stage}", [
        {"type": "section", "text": {"type": "mrkdwn", "text": _cap("\n".join(lines))}},
        {"type": "actions", "elements": elements},
    ]


def _section(text: str) -> dict:
    return {"type": "section", "text": {"type": "mrkdwn", "text": _cap(text)}}


def ask_blocks(
    ref: str, *, owner: Optional[UserExtra], company: str, stage: str, end: datetime,
) -> tuple[str, list[dict]]:
    """The after-call ask posted in the interview's thread once a caller round has ended."""
    text = (f"{_who(owner, 'Owner')} — {_short(company, MAX_NAME, '—')} {_stage_label(stage)} ended at "
            f"{slack_time(end, with_day=False)}. How did it go?")
    return text, [_section(text), {"type": "actions", "elements": [
        _button("Completed", style="primary", action_id=COORD_AFTER_COMPLETED, value=ref),
        _button("Canceled", action_id=COORD_AFTER_CANCELED, value=ref),
        _button("Rescheduled…", action_id=COORD_AFTER_RESCHEDULED, value=ref),
    ]}]


def result_reminder_blocks(
    ref: str, *, owner: Optional[UserExtra], company: str, stage: str, start: datetime,
) -> tuple[str, list[dict]]:
    """The reminder posted in the thread when a completed round has had no result for 7 days."""
    text = (f"{_who(owner, 'Owner')} — {_short(company, MAX_NAME, '—')} {_stage_label(stage)} on "
            f"{slack_time(start)} has no result yet. How did it end?")
    return text, [_section(text), {"type": "actions", "elements": [
        _button("Passed", style="primary", action_id=COORD_RESULT_PASSED, value=ref),
        _button("Rejected", style="danger", action_id=COORD_RESULT_REJECTED, value=ref),
    ]}]


def answered_blocks(line: str) -> list[dict]:
    """What replaces an ask once it is answered: the answer line alone, no buttons."""
    return [_section(line)]
