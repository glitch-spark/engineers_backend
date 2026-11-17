"""Slack clicks in the coordination channel: the click model, the dispatcher and the reply helpers that the
handlers (Tasks 6 and 9) share. Handlers never raise out of `process_coordination_click`."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional

from app import slack_bot
from app.models import UserExtra
from app.slack_transactions import _post_ephemeral

log = logging.getLogger(__name__)

NOT_LINKED = "Link your Slack in Account settings first."
TRY_AGAIN = "Something went wrong. Try again from the app."
FORM_FAILED = "Couldn't open the form — click again."


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
