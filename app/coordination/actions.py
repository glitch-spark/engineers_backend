"""Slack clicks in the coordination channel: the click model, the dispatcher and the reply helpers that the
handlers (Tasks 6 and 9) share. Handlers never raise out of `process_coordination_click`."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Optional

from fastapi import HTTPException

from app import slack_bot
from app.caller_access import can_update_round
from app.coordination.blocks import (
    COORD_AFTER_CANCELED,
    COORD_AFTER_COMPLETED,
    COORD_AFTER_RESCHEDULED,
    COORD_CHOOSE_CALLER,
    COORD_CONFIRM,
    COORD_RESULT_PASSED,
    COORD_RESULT_REJECTED,
    answered_blocks,
)
from app.coordination.modals import Origin, cancel_view, reject_view, reschedule_view
from app.interview_day.blocks import _short, _who
from app.interview_normalize import interview_status_value
from app.models import Interview, ResultRoundupPost, UserExtra
from app.models.interview import InterviewStageEvent
from app.interview_caller import CallerIn
from app.round_confirm import ConfirmError, confirm_round
from app.routers.callers import is_caller_manager
from app.slack_transactions import _post_ephemeral

log = logging.getLogger(__name__)

NOT_LINKED = "Link your Slack in Account settings first."
TRY_AGAIN = "Something went wrong. Try again from the app."
FORM_FAILED = "Couldn't open the form — click again."
ROUND_GONE = "This round is gone."
CHOOSE_REFUSED = "Only the owner or an interview manager can assign the caller."
SELF_PICKED = "No caller (Self) — the caller request is off."
UPDATE_REFUSED = "Only the owner or an interview manager can update this round."
NEEDS_COMPLETED = "Mark the round completed first."
ALREADY = "Already updated: {Label}."
ALREADY_CONFIRMED = "Already confirmed."
MOVED = "This round was moved — answer the new ask after it ends."


@dataclass(frozen=True)
class Click:
    action_id: str
    value: str
    slack_user_id: str
    response_url: str
    trigger_id: str
    channel: str
    message_ts: str


def _dict(value: object) -> dict:
    return value if isinstance(value, dict) else {}


def click_from_payload(payload: dict) -> Click:
    """The fields of a `block_actions` payload the handlers need; a missing piece is an empty string."""
    actions = payload.get("actions")
    action = _dict(actions[0]) if isinstance(actions, list) and actions else {}
    value = action.get("value")
    if not value:
        value = _dict(action.get("selected_option")).get("value")
    container = _dict(payload.get("container"))
    channel = _dict(payload.get("channel")).get("id") or container.get("channel_id")
    message_ts = container.get("message_ts") or _dict(payload.get("message")).get("ts")
    return Click(
        action_id=str(action.get("action_id") or ""),
        value=str(value or ""),
        slack_user_id=str(_dict(payload.get("user")).get("id") or ""),
        response_url=str(payload.get("response_url") or ""),
        trigger_id=str(payload.get("trigger_id") or ""),
        channel=str(channel or ""),
        message_ts=str(message_ts or ""),
    )


async def slack_platform_user(slack_user_id: str) -> Optional[UserExtra]:
    """The active platform user whose Slack member id this is; None for a blank id."""
    slack_id = (slack_user_id or "").strip()
    if not slack_id:
        return None
    return await UserExtra.find_one({"slackUserId": slack_id, "isActive": {"$ne": False}})


async def reply(response_url: str, text: str) -> None:
    """An ephemeral note to the clicker; the clicked message stays as it is."""
    await _post_ephemeral(response_url, {"response_type": "ephemeral", "replace_original": False, "text": text})


async def replace(response_url: str, text: str, blocks: list[dict]) -> None:
    """Replace the clicked message in place."""
    await _post_ephemeral(
        response_url, {"response_type": "in_channel", "replace_original": True, "text": text, "blocks": blocks},
    )


async def open_modal(trigger_id: str, view: dict) -> bool:
    """`views.open` with the click's trigger id. False (and a log line) on any error."""
    try:
        await slack_bot._slack_api("views.open", {"trigger_id": trigger_id, "view": view})
        return True
    except Exception as exc:
        log.warning("coordination: views.open failed: %s", exc)
        return False


async def tell(channel: str, slack_user_id: str, text: str) -> None:
    """An ephemeral message in the channel for one user, for results with no response_url. Never raises."""
    try:
        await slack_bot._slack_api("chat.postEphemeral", {"channel": channel, "user": slack_user_id, "text": text})
    except Exception as exc:
        log.warning("coordination: chat.postEphemeral to %s failed: %s", slack_user_id, exc)


HANDLERS: dict[str, Callable[[Click, UserExtra], Awaitable[None]]] = {}


async def _find_round(iv_id: str, round_id: str) -> Optional[tuple[Interview, InterviewStageEvent]]:
    """The interview and its round, or None when either is gone (or the id is not an object id)."""
    from app.routers import interviews as ivr

    try:
        iv = await Interview.get(ivr.to_object_id(iv_id))
    except HTTPException:
        return None
    if iv is None:
        return None
    entry = next((e for e in iv.stageHistory if e.id == round_id), None)
    return (iv, entry) if entry is not None else None


def _caller_name(out: dict, round_id: str) -> str:
    """The caller's name on the round in an interview payload returned by a router function."""
    for entry in out.get("stageHistory") or []:
        if isinstance(entry, dict) and entry.get("id") == round_id:
            return str(((entry.get("caller") or {}).get("callerName")) or "").strip() or "TBD"
    return "TBD"


async def choose_caller(click: Click, user: UserExtra) -> None:
    """The caller select on the post. Managers go through the manager endpoint; the owner through the round's
    `caller` field, which keeps the round's method and link."""
    from app.routers import interviews as ivr

    parts = click.value.split(":")
    if len(parts) != 3 or not all(parts):
        await reply(click.response_url, ROUND_GONE)
        return
    iv_id, round_id, pick = parts
    found = await _find_round(iv_id, round_id)
    if found is None:
        await reply(click.response_url, ROUND_GONE)
        return
    iv, entry = found
    is_self = pick == "self"
    try:
        if is_caller_manager(user):
            body = ivr.CallerAssignIn(callerId="", enabled=False) if is_self else ivr.CallerAssignIn(
                callerId=pick, enabled=True,
            )
            out = await ivr.assign_round_caller(iv_id, round_id, body, user)
        elif str(iv.createdBy) == str(user.id):
            prev = entry.caller
            caller = CallerIn(enabled=False) if is_self else CallerIn(
                enabled=True, callerId=pick,
                method=prev.method if prev else None, methodValue=prev.methodValue if prev else "",
            )
            out = await ivr.update_interview_stage(iv_id, round_id, ivr.StagePatch(caller=caller), user)
        else:
            await reply(click.response_url, CHOOSE_REFUSED)
            return
    except HTTPException as exc:
        await reply(click.response_url, str(exc.detail))
        return
    await reply(click.response_url, SELF_PICKED if is_self else f"Caller set: {_caller_name(out, round_id)}.")


async def confirm(click: Click, user: UserExtra) -> None:
    """The Confirm button. `confirm_round` syncs the post, which then shows "confirmed"; only a refusal replies."""
    parts = click.value.split(":")
    if len(parts) != 2 or not all(parts):
        await reply(click.response_url, ROUND_GONE)
        return
    found = await _find_round(parts[0], parts[1])
    if found is not None and found[1].confirmed:  # an old click on a round confirmed since: still answered
        await reply(click.response_url, ALREADY_CONFIRMED)
        return
    try:
        await confirm_round(parts[0], parts[1], user, True)
    except ConfirmError as exc:
        await reply(click.response_url, exc.message)


HANDLERS[COORD_CHOOSE_CALLER] = choose_caller
HANDLERS[COORD_CONFIRM] = confirm


# --- after-call answers and results ----------------------------------------------------------------------------


async def load_round(ref: str) -> tuple[Interview, InterviewStageEvent]:
    """The interview and round named by "<interviewId>:<roundId>". `LookupError` when malformed or gone."""
    parts = (ref or "").split(":")
    if len(parts) != 2 or not all(parts):
        raise LookupError(ref)
    found = await _find_round(parts[0], parts[1])
    if found is None:
        raise LookupError(ref)
    return found


async def set_round(iv_id: str, round_id: str, user: UserExtra, **fields: Any) -> None:
    """The app's round write. Only the given fields are sent, so a manager's PATCH carries nothing it may not."""
    from app.routers import interviews as ivr

    await ivr.update_interview_stage(iv_id, round_id, ivr.StagePatch(**fields), user)


def round_status(entry: InterviewStageEvent) -> str:
    """The round's status as the post reads it; a missing status is scheduled."""
    return interview_status_value(entry.status) or "scheduled"


def _label(status: str) -> str:
    return status.capitalize()


_ANSWER_HEADS = {
    "completed": "✅ Completed", "canceled": "❌ Canceled", "passed": "🎉 Passed", "rejected": "❌ Rejected",
}


def answer_line(kind: str, user: UserExtra, *, note: str = "", new_start: Optional[datetime] = None) -> str:
    """What an answered ask shows."""
    head = f"🔁 Rescheduled to {slack_bot.slack_time(new_start)}" if kind == "rescheduled" else _ANSWER_HEADS[kind]
    line = f"{head} — by {_who(user)}"
    if kind == "canceled" and note:
        line += f" · {_short(note, 200)}"
    return line


async def show_answer(iv: Interview, *, channel: str, ts: str, response_url: str, line: str) -> None:
    """Replace the answered ask (or reminder) with its answer line. The coordination post itself is left alone: the
    write already re-rendered it. Never raises."""
    try:
        if ts:
            roundup = await ResultRoundupPost.find_one({"ts": ts})
            if roundup is not None:  # answered on the Friday roundup: it drops the row, edited in place
                from app.coordination.chase import refresh_roundup  # chase imports the interview router

                await refresh_roundup(roundup)
                return
        thread = iv.slackThread
        if thread is not None and ts and ts == thread.ts:
            return
        if response_url:
            await replace(response_url, line, answered_blocks(line))
        elif channel and ts:
            await slack_bot._slack_api(
                "chat.update", {"channel": channel, "ts": ts, "text": line, "blocks": answered_blocks(line)},
            )
    except Exception as exc:
        log.warning("coordination: showing the answer on %s failed: %s", ts, exc)


async def _clicked_round(
    click: Click, user: UserExtra,
) -> Optional[tuple[Interview, InterviewStageEvent]]:
    """The clicked round when the clicker may update it; otherwise the refusal is replied and None returned."""
    try:
        iv, entry = await load_round(click.value)
    except LookupError:
        await reply(click.response_url, ROUND_GONE)
        return None
    if not can_update_round(iv, user):
        await reply(click.response_url, UPDATE_REFUSED)
        return None
    return iv, entry


async def _ask_still_open(click: Click, iv: Interview, entry: InterviewStageEvent) -> bool:
    """The checks shared by the three after-call answers: the round is still scheduled, and has really ended (an old
    ask answered after a reschedule must not touch the moved round). Replies and returns False otherwise."""
    from app.routers import interviews as ivr

    status = round_status(entry)
    if status != "scheduled":
        await reply(click.response_url, ALREADY.format(Label=_label(status)))
        await show_answer(iv, channel=click.channel, ts=click.message_ts, response_url=click.response_url,
                          line=f"Already updated: {_label(status)}")
        return False
    if entry.scheduledAt is not None:
        end = slack_bot._aware_utc(ivr.round_ends_at(entry.scheduledAt, entry.endsAt))
        if end > datetime.now(timezone.utc):
            await reply(click.response_url, MOVED)
            await show_answer(iv, channel=click.channel, ts=click.message_ts, response_url=click.response_url,
                              line=MOVED)
            return False
    return True


async def _write_and_show(click: Click, user: UserExtra, iv: Interview, entry: InterviewStageEvent, kind: str) -> None:
    try:
        await set_round(str(iv.id), entry.id, user, status=kind)
    except HTTPException as exc:
        await reply(click.response_url, str(exc.detail))
        return
    await show_answer(iv, channel=click.channel, ts=click.message_ts, response_url=click.response_url,
                      line=answer_line(kind, user))


async def _open_form(click: Click, iv: Interview, entry: InterviewStageEvent, build: Callable[..., dict],
                     **extra: Any) -> None:
    owner = await UserExtra.get(iv.createdBy) if iv.createdBy else None
    origin = Origin(click.value, click.channel, click.message_ts, tz=slack_bot.resolve_user_tz(owner).key)
    title = f"{iv.companyName or '—'} · {slack_bot._stage_label(entry.stage)}"
    if not await open_modal(click.trigger_id, build(origin, title, **extra)):
        await reply(click.response_url, FORM_FAILED)


async def after_completed(click: Click, user: UserExtra) -> None:
    found = await _clicked_round(click, user)
    if found is None or not await _ask_still_open(click, *found):
        return
    await _write_and_show(click, user, *found, "completed")


async def after_canceled(click: Click, user: UserExtra) -> None:
    found = await _clicked_round(click, user)
    if found is None or not await _ask_still_open(click, *found):
        return
    await _open_form(click, *found, cancel_view)


async def after_rescheduled(click: Click, user: UserExtra) -> None:
    found = await _clicked_round(click, user)
    if found is None or not await _ask_still_open(click, *found):
        return
    iv, entry = found
    await _open_form(click, iv, entry, reschedule_view, start=entry.scheduledAt or datetime.now(timezone.utc))


async def _result_open(click: Click, iv: Interview, entry: InterviewStageEvent) -> bool:
    """A result can be set only on a completed round without one. Replies and returns False otherwise."""
    status = round_status(entry)
    if status in ("passed", "rejected"):
        await reply(click.response_url, f"Result already set: {_label(status)}.")
        # An old Friday roundup row for a round whose result was set in the app: drop it from the roundup.
        await show_answer(iv, channel=click.channel, ts=click.message_ts, response_url=click.response_url,
                          line=f"Result already set: {_label(status)}")
        return False
    if status != "completed":
        await reply(click.response_url, NEEDS_COMPLETED)
        return False
    return True


async def result_passed(click: Click, user: UserExtra) -> None:
    found = await _clicked_round(click, user)
    if found is None or not await _result_open(click, *found):
        return
    await _write_and_show(click, user, *found, "passed")


async def result_rejected(click: Click, user: UserExtra) -> None:
    found = await _clicked_round(click, user)
    if found is None or not await _result_open(click, *found):
        return
    await _open_form(click, *found, reject_view)


HANDLERS[COORD_AFTER_COMPLETED] = after_completed
HANDLERS[COORD_AFTER_CANCELED] = after_canceled
HANDLERS[COORD_AFTER_RESCHEDULED] = after_rescheduled
HANDLERS[COORD_RESULT_PASSED] = result_passed
HANDLERS[COORD_RESULT_REJECTED] = result_rejected


async def process_coordination_click(click: Click) -> None:
    """Resolve the clicker and run the handler for the action. Never raises: a crash replies `TRY_AGAIN`."""
    try:
        user = await slack_platform_user(click.slack_user_id)
        if user is None:
            await reply(click.response_url, NOT_LINKED)
            return
        handler = HANDLERS.get(click.action_id)
        if handler is None:
            log.warning("coordination: no handler for action %r", click.action_id)
            return
        await handler(click, user)
    except Exception as exc:
        log.warning("coordination click %r failed: %s", click.action_id, exc)
        try:
            await reply(click.response_url, TRY_AGAIN)
        except Exception as inner:
            log.warning("coordination: reply failed: %s", inner)
