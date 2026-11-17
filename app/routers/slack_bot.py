"""Slack interview bot routes: Slack member ID and prefs, test DM, button clicks (interactions), cron tick."""

from __future__ import annotations

import json
import logging
import time
from typing import Annotated, Optional

from urllib.parse import parse_qs

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException, Request, status
from pydantic import BaseModel

from app.config import get_settings
from app.deps import get_current_user
from app.interview_day.actions import process_confirm_click
from app.interview_day.blocks import CONFIRM_ACTION_ID
from app.models import UserExtra
from app.slack_bot import (
    normalize_slack_id,
    normalize_slack_timezone,
    run_alert_tick,
    send_dm,
    slack_bot_configured,
    validate_timezone,
)
from app.slack_timezones import slack_timezone_catalog
from app.slack_transactions import is_transaction_action, process_transaction_click, verify_slack_signature


log = logging.getLogger(__name__)

router = APIRouter(prefix="/integrations/slack", tags=["slack-bot"])
cron_router = APIRouter(prefix="/internal/slack-bot", tags=["slack-bot"])

def _slack_profile_fields(user: UserExtra) -> dict:
    connected = bool((user.slackUserId or "").strip())
    s = get_settings()
    tz_name = normalize_slack_timezone(
        (user.slackTimezone or "").strip()
        or (s.APP_TIMEZONE or "").strip()
        or "America/New_York"
    )
    return {
        "slackConnected": connected,
        "slackUserId": (user.slackUserId or "").strip() or None,
        "slackTimezone": tz_name,
        "slackTimezones": slack_timezone_catalog(),
        "slackBotConfigured": slack_bot_configured(),
    }


class SlackPrefsUpdate(BaseModel):
    # The user's Slack member ID (U…); blank clears it.
    slackUserId: Optional[str] = None
    slackTimezone: Optional[str] = None


@router.get("/status")
async def slack_status(user: Annotated[UserExtra, Depends(get_current_user)]) -> dict:
    return _slack_profile_fields(user)


@router.patch("/prefs")
async def slack_prefs(
    body: SlackPrefsUpdate,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    if "slackUserId" in body.model_fields_set:
        try:
            slack_id = normalize_slack_id(body.slackUserId)
        except ValueError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
        # One Slack account → one Engineers user.
        if slack_id and await UserExtra.find_one({"slackUserId": slack_id, "_id": {"$ne": user.id}}):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail="That Slack ID is already used by another user",
            )
        user.slackUserId = slack_id
    if body.slackTimezone is not None:
        try:
            user.slackTimezone = validate_timezone(body.slackTimezone)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc
    await user.save()
    return _slack_profile_fields(user)


@router.post("/test-dm")
async def slack_test_dm(user: Annotated[UserExtra, Depends(get_current_user)]) -> dict:
    if not slack_bot_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="SLACK_BOT_TOKEN is not configured",
        )
    slack_id = (user.slackUserId or "").strip()
    if not slack_id:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Add your Slack member ID before sending a test DM",
        )
    ok = await send_dm(
        slack_id,
        text=(
            ":white_check_mark: *Engineers bot connected* — your member ID works. You'll be tagged in #caller "
            "threads and the daily caller interviews list."
        ),
    )
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to send Slack DM — open a chat with the bot once and retry",
        )
    return {"ok": True}


@router.post("/interactions")
async def slack_interactions(request: Request, background: BackgroundTasks) -> dict:
    """Slack button clicks. No app login: Slack signs the request with SLACK_SIGNING_SECRET, checked over the raw
    body before anything is parsed. Handled clicks are acknowledged at once and applied in the background
    (Slack wants an answer within 3 seconds); refusals reach the clicker as ephemeral text."""
    secret = (get_settings().SLACK_SIGNING_SECRET or "").strip()
    if not secret:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="SLACK_SIGNING_SECRET is not configured",
        )
    body = await request.body()
    if not verify_slack_signature(
        secret,
        request.headers.get("X-Slack-Request-Timestamp", ""),
        body,
        request.headers.get("X-Slack-Signature", ""),
        now=time.time(),
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid signature")
    try:
        payload = json.loads(parse_qs(body.decode("utf-8")).get("payload", [""])[0])
    except (UnicodeDecodeError, ValueError):
        return {"ok": True}
    if not isinstance(payload, dict) or payload.get("type") != "block_actions":
        return {"ok": True}
    actions = payload.get("actions") or []
    action = actions[0] if actions and isinstance(actions[0], dict) else {}
    action_id = str(action.get("action_id") or "")
    user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
    value = str(action.get("value") or "")
    slack_user_id = str(user.get("id") or "")
    response_url = str(payload.get("response_url") or "")
    message = payload.get("message") if isinstance(payload.get("message"), dict) else {}
    if action_id == CONFIRM_ACTION_ID:
        background.add_task(process_confirm_click, value, slack_user_id, response_url, str(message.get("ts") or ""))
    elif is_transaction_action(action_id):
        background.add_task(process_transaction_click, action_id, value, slack_user_id, response_url)
    return {"ok": True}


@cron_router.post("/tick")
async def slack_bot_tick(
    x_cron_secret: Annotated[Optional[str], Header()] = None,
) -> dict:
    s = get_settings()
    expected = (s.SLACK_BOT_CRON_SECRET or "").strip()
    if not expected or (x_cron_secret or "").strip() != expected:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")
    result = await run_alert_tick()
    return {"ok": True, **result}
