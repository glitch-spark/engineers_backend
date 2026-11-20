"""Slack forms (modals) for the coordination channel: the three views, and reading and checking a submission.
Task 9 fills in `process_coordination_submission`.

`modals` never imports `actions` at module level: `actions` imports this module, and the submission handler needs the
`actions` helpers, so it imports them inside the function."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timezone
from typing import Literal, Optional
from zoneinfo import ZoneInfo

from app.interview_day.blocks import _short
from app.interview_reasons import CANCEL_REASON_HELP, CANCEL_REASONS, REJECT_REASONS

log = logging.getLogger(__name__)

CANCEL_VIEW = "coord_cancel"
RESCHEDULE_VIEW = "coord_reschedule"
REJECT_VIEW = "coord_reject"
COORD_VIEWS = frozenset({CANCEL_VIEW, RESCHEDULE_VIEW, REJECT_VIEW})

MAX_NOTE = 2000
NOTE_TOO_LONG = f"At most {MAX_NOTE} characters."
FORM_ERROR = "Something went wrong. Try again from the app."

_TITLES = {CANCEL_VIEW: "Canceled", RESCHEDULE_VIEW: "Rescheduled", REJECT_VIEW: "Rejected"}
_NOTE_LABEL = "Details (optional)"
_REASON_LABELS = {
    CANCEL_VIEW: "Why was it canceled?", RESCHEDULE_VIEW: "Why was it rescheduled?", REJECT_VIEW: "Why was it rejected?",
}
_KINDS = {CANCEL_VIEW: "canceled", RESCHEDULE_VIEW: "rescheduled", REJECT_VIEW: "rejected"}


@dataclass(frozen=True)
class Origin:
    """Where a form was opened from: the round ("<interviewId>:<roundId>"), the clicked post, and the owner's zone."""

    ref: str
    channel: str
    ts: str
    tz: str = ""

    def to_metadata(self) -> str:
        return json.dumps({"ref": self.ref, "channel": self.channel, "ts": self.ts, "tz": self.tz}, sort_keys=True)

    @classmethod
    def from_metadata(cls, raw: str) -> "Origin":
        try:
            data = json.loads(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError("malformed private_metadata") from exc
        if not isinstance(data, dict):
            raise ValueError("malformed private_metadata")
        ref = data.get("ref")
        if not isinstance(ref, str) or ref.count(":") != 1 or not all(ref.split(":")):
            raise ValueError("malformed ref")
        rest = [data.get(key, "") for key in ("channel", "ts", "tz")]
        if not all(isinstance(part, str) for part in rest):
            raise ValueError("malformed private_metadata")
        return cls(ref, *rest)


@dataclass(frozen=True)
class Answer:
    kind: Literal["canceled", "rescheduled", "rejected"]
    origin: Origin
    note: str  # stripped
    new_start: Optional[datetime]  # aware UTC; set only for a reschedule
    reason: str = ""  # the picked reason key; "" from a form opened before the forms asked for one


def _plain(text: str) -> dict:
    return {"type": "plain_text", "text": text}


def _zone(tz: str) -> ZoneInfo:
    """The owner's zone; UTC when it is empty or unknown, so a form still opens and reads back the same way."""
    try:
        return ZoneInfo(tz or "UTC")
    except Exception:  # noqa: BLE001 - unknown or malformed zone keys
        return ZoneInfo("UTC")


def _note_block(callback_id: str) -> dict:
    return {
        "type": "input",
        "block_id": "note",
        "optional": True,
        "label": _plain(_NOTE_LABEL),
        "element": {"type": "plain_text_input", "action_id": "note_input", "multiline": True, "max_length": MAX_NOTE},
    }


def _reason_block(callback_id: str) -> dict:
    """The required reason select (app.interview_reasons): canceled and rescheduled share a list, with helper texts."""
    reasons = REJECT_REASONS if callback_id == REJECT_VIEW else CANCEL_REASONS
    options = [{"text": _plain(label), "value": key} for key, label in reasons.items()]
    if reasons is CANCEL_REASONS:
        for option in options:
            option["description"] = _plain(CANCEL_REASON_HELP[option["value"]])
    return {
        "type": "input",
        "block_id": "reason",
        "label": _plain(_REASON_LABELS[callback_id]),
        "element": {"type": "static_select", "action_id": "reason_input", "options": options},
    }


def _picked_reason(view: dict) -> str:
    """The reason select's value; "" when the form has no reason block or nothing is picked."""
    try:
        option = view["state"]["values"]["reason"]["reason_input"].get("selected_option")
    except (KeyError, TypeError, AttributeError):
        return ""
    value = option.get("value") if isinstance(option, dict) else None
    return value if isinstance(value, str) else ""


def _view(callback_id: str, origin: Origin, title_line: str, middle: list[dict]) -> dict:
    return {
        "type": "modal",
        "callback_id": callback_id,
        "private_metadata": origin.to_metadata(),
        "title": _plain(_TITLES[callback_id]),
        "submit": _plain("Save"),
        "close": _plain("Back"),
        "blocks": [
            {"type": "context", "elements": [{"type": "mrkdwn", "text": _short(title_line, 200)}]},
            _reason_block(callback_id),
            *middle,
            _note_block(callback_id),
        ],
    }


def cancel_view(origin: Origin, title_line: str) -> dict:
    return _view(CANCEL_VIEW, origin, title_line, [])


def reject_view(origin: Origin, title_line: str) -> dict:
    return _view(REJECT_VIEW, origin, title_line, [])


def reschedule_view(origin: Origin, title_line: str, *, start: datetime) -> dict:
    """Date and time default to the round's current start, read in the owner's zone."""
    if start.tzinfo is None:
        start = start.replace(tzinfo=timezone.utc)
    local = start.astimezone(_zone(origin.tz))
    middle = [
        {
            "type": "input",
            "block_id": "date",
            "label": _plain("Date"),
            "element": {"type": "datepicker", "action_id": "date_input", "initial_date": local.date().isoformat()},
        },
        {
            "type": "input",
            "block_id": "time",
            "label": _plain(f"Time ({_zone(origin.tz).key})"),
            "element": {"type": "timepicker", "action_id": "time_input", "initial_time": local.strftime("%H:%M")},
        },
    ]
    return _view(RESCHEDULE_VIEW, origin, title_line, middle)


def _value(view: dict, block: str, action: str, key: str) -> Optional[str]:
    try:
        found = view["state"]["values"][block][action].get(key)
    except (KeyError, TypeError, AttributeError):
        raise ValueError(f"missing {block}.{action}") from None
    if found is not None and not isinstance(found, str):
        raise ValueError(f"malformed {block}.{action}")
    return found


def _new_start(origin: Origin, picked_date: str, picked_time: str) -> datetime:
    try:
        local = datetime.combine(date.fromisoformat(picked_date), time.fromisoformat(picked_time))
    except ValueError as exc:
        raise ValueError("malformed date or time") from exc
    return local.replace(tzinfo=_zone(origin.tz)).astimezone(timezone.utc)


def read_submission(view: dict) -> Answer:
    """The answer a submitted form carries. `ValueError` when the form is malformed."""
    if not isinstance(view, dict):
        raise ValueError("malformed view")
    callback_id = view.get("callback_id")
    if callback_id not in COORD_VIEWS:
        raise ValueError("not a coordination form")
    origin = Origin.from_metadata(view.get("private_metadata") or "")
    note = (_value(view, "note", "note_input", "value") or "").strip()
    new_start = None
    if callback_id == RESCHEDULE_VIEW:
        picked_date = _value(view, "date", "date_input", "selected_date")
        picked_time = _value(view, "time", "time_input", "selected_time")
        if not picked_date or not picked_time:
            raise ValueError("date or time missing")
        new_start = _new_start(origin, picked_date, picked_time)
    return Answer(_KINDS[callback_id], origin, note, new_start, _picked_reason(view))


def submission_errors(view: dict, now: datetime) -> dict[str, str]:
    """Field errors to show inline in the form; empty when the submission is fine.

    Never raises: the router calls this inline, and a raise is a 500 that shows Slack's generic error in the modal.
    Malformed metadata gives `{}` (the background handler reports it); any other surprise gives one generic error on
    the note field, which every form has."""
    try:
        try:
            origin = Origin.from_metadata(view.get("private_metadata") or "")
            note = _value(view, "note", "note_input", "value") or ""
            callback_id = view.get("callback_id")
            picked_date = picked_time = None
            if callback_id == RESCHEDULE_VIEW:
                picked_date = _value(view, "date", "date_input", "selected_date")
                picked_time = _value(view, "time", "time_input", "selected_time")
        except ValueError:
            return {}
        errors: dict[str, str] = {}
        if callback_id == RESCHEDULE_VIEW:
            if not picked_date:
                errors["date"] = "Pick a date."
            if not picked_time:
                errors["time"] = "Pick a time."
            if picked_date and picked_time:
                try:
                    if _new_start(origin, picked_date, picked_time) <= now:
                        errors["time"] = "Pick a time in the future."
                except ValueError:
                    errors["date"] = "Pick a date."
        if len(note.strip()) > MAX_NOTE:
            errors["note"] = NOTE_TOO_LONG
        return errors
    except Exception:  # noqa: BLE001
        log.exception("coordination form validation failed")
        return {"note": FORM_ERROR}


# Rejected comes from a completed round's result buttons, or straight from the after-interview check (still scheduled).
_NEEDED_STATUS = {"canceled": {"scheduled"}, "rescheduled": {"scheduled"}, "rejected": {"completed", "scheduled"}}


async def process_coordination_submission(view: dict, slack_user_id: str) -> None:
    """Apply a submitted form through the app's round write; the post re-renders through its sync, and the answered
    ask (or reminder) is replaced with its answer line. Runs after the form closed, so every check is made again
    here and the submitter hears any refusal ephemerally. Never raises."""
    from fastapi import HTTPException

    from app import slack_bot
    from app.coordination import actions
    from app.caller_access import can_update_round

    channel = ""
    try:
        try:
            answer = read_submission(view)
        except ValueError as exc:
            log.warning("coordination submission from %s is malformed: %s", slack_user_id, exc)
            raw = view.get("private_metadata") if isinstance(view, dict) else None
            try:
                channel = Origin.from_metadata(raw or "").channel
            except ValueError:
                channel = ""
            if channel:
                await actions.tell(channel, slack_user_id, actions.TRY_AGAIN)
            return
        origin = answer.origin
        channel = origin.channel

        async def tell(text: str) -> None:
            await actions.tell(origin.channel, slack_user_id, text)

        user = await actions.slack_platform_user(slack_user_id)
        if user is None:
            await tell(actions.NOT_LINKED)
            return
        try:
            iv, entry = await actions.load_round(origin.ref)
        except LookupError:
            await tell(actions.ROUND_GONE)
            return
        if not can_update_round(iv, user):
            await tell(actions.UPDATE_REFUSED)
            return
        status = actions.round_status(entry)
        if status not in _NEEDED_STATUS[answer.kind]:
            await tell(actions.ALREADY.format(Label=status.capitalize()))
            return
        if status == "scheduled" and entry.scheduledAt is not None:
            # A form opened from an old ask must not apply to a round that was moved since and hasn't ended.
            from app.routers.interviews import round_ends_at

            end = slack_bot._aware_utc(round_ends_at(entry.scheduledAt, entry.endsAt))
            if end > datetime.now(timezone.utc):
                await tell(actions.MOVED)
                return

        iv_id, round_id = origin.ref.split(":")
        try:
            if answer.kind == "rescheduled":
                await actions.set_round(
                    iv_id, round_id, user, scheduledAt=answer.new_start.isoformat(),
                    **({"statusNote": answer.note} if answer.note else {}),
                    **({"rescheduleReason": answer.reason, "rescheduleNote": answer.note} if answer.reason else {}),
                )
            else:
                await actions.set_round(
                    iv_id, round_id, user, status=answer.kind, statusNote=answer.note,
                    **({"statusReason": answer.reason} if answer.reason else {}),
                )
        except HTTPException as exc:
            await tell(str(exc.detail))
            return

        if answer.kind == "rescheduled":
            # Show the time actually saved (a time in a DST gap moves forward), in the owner's zone.
            iv, entry = await actions.load_round(origin.ref)
            saved = slack_bot._aware_utc(entry.scheduledAt) if entry.scheduledAt else answer.new_start
            line = actions.answer_line("rescheduled", user, new_start=saved)
            await actions.show_answer(iv, channel=origin.channel, ts=origin.ts, response_url="", line=line)
            await tell(f"Rescheduled to {slack_bot.fmt_caller_time(saved, _zone(origin.tz))}.")
            return
        line = actions.answer_line(answer.kind, user, note=answer.note)
        await actions.show_answer(iv, channel=origin.channel, ts=origin.ts, response_url="", line=line)
    except Exception as exc:  # noqa: BLE001 - a background task: log, tell, never raise
        log.exception("coordination submission from %s failed: %s", slack_user_id, exc)
        if channel:
            try:
                from app.coordination import actions

                await actions.tell(channel, slack_user_id, actions.TRY_AGAIN)
            except Exception:  # noqa: BLE001
                pass
