"""Slack interview bot routes: OAuth connect, prefs, test DM, cron tick."""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Annotated, Optional
from urllib.parse import urlencode

from beanie import PydanticObjectId
from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from jose import JWTError, jwt
from pydantic import BaseModel, Field

from app.config import get_settings
from app.deps import get_current_user
from app.models import UserExtra
from app.slack_bot import (
    build_oauth_authorize_url,
    exchange_oauth_code,
    run_alert_tick,
    send_dm,
    slack_bot_configured,
    slack_oauth_configured,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/integrations/slack", tags=["slack-bot"])
cron_router = APIRouter(prefix="/internal/slack-bot", tags=["slack-bot"])

_STATE_AUDIENCE = "slack-oauth"
_STATE_TTL_SECONDS = 600


def _sign_state(user_id: PydanticObjectId) -> str:
    s = get_settings()
    payload = {
        "sub": str(user_id),
        "aud": _STATE_AUDIENCE,
        "exp": datetime.now(timezone.utc) + timedelta(seconds=_STATE_TTL_SECONDS),
        "jti": secrets.token_urlsafe(16),
    }
    return jwt.encode(payload, s.AUTH_SECRET, algorithm="HS256")


def _verify_state(token: str) -> PydanticObjectId:
    s = get_settings()
    try:
        payload = jwt.decode(
            token, s.AUTH_SECRET, algorithms=["HS256"], audience=_STATE_AUDIENCE
        )
        return PydanticObjectId(payload["sub"])
    except (JWTError, KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="invalid or expired state") from exc


def _frontend_redirect(*, status_value: str, detail: Optional[str] = None) -> RedirectResponse:
    s = get_settings()
    qs = {"slack": status_value}
    if detail:
        qs["detail"] = detail[:200]
    return RedirectResponse(
        url=f"{s.FRONTEND_ORIGIN}/profile?{urlencode(qs)}",
        status_code=302,
    )


def _slack_profile_fields(user: UserExtra) -> dict:
    connected = bool((user.slackUserId or "").strip())
    s = get_settings()
    return {
        "slackConnected": connected,
        "slackAlertsEnabled": bool(user.slackAlertsEnabled),
        "slackDigestHour": (
            user.slackDigestHour
            if user.slackDigestHour is not None
            else s.SLACK_DIGEST_HOUR
        ),
        "slackDigestMinute": (
            user.slackDigestMinute
            if user.slackDigestMinute is not None
            else s.SLACK_DIGEST_MINUTE
        ),
        "slackOAuthConfigured": slack_oauth_configured(),
        "slackBotConfigured": slack_bot_configured(),
    }


class SlackPrefsUpdate(BaseModel):
    slackAlertsEnabled: Optional[bool] = None
    slackDigestHour: Optional[int] = Field(default=None, ge=0, le=23)
    slackDigestMinute: Optional[int] = Field(default=None, ge=0, le=59)


@router.get("/status")
async def slack_status(user: Annotated[UserExtra, Depends(get_current_user)]) -> dict:
    return _slack_profile_fields(user)


@router.post("/oauth-start")
async def slack_oauth_start(user: Annotated[UserExtra, Depends(get_current_user)]) -> dict:
    if not slack_oauth_configured():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Slack OAuth is not configured on the server",
        )
    url = build_oauth_authorize_url(state=_sign_state(user.id))
    return {"url": url}


@router.get("/oauth-callback")
async def slack_oauth_callback(
    code: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
):
    if error:
        return _frontend_redirect(status_value="error", detail=error)
    if not code or not state:
        return _frontend_redirect(status_value="error", detail="missing code or state")
    try:
        user_id = _verify_state(state)
    except HTTPException:
        return _frontend_redirect(status_value="error", detail="invalid state")
    user = await UserExtra.get(user_id)
    if user is None:
        return _frontend_redirect(status_value="error", detail="user not found")
    try:
        slack_uid = await exchange_oauth_code(code)
    except Exception as exc:
        log.warning("slack oauth-callback failed for %s: %s", user_id, exc)
        return _frontend_redirect(status_value="error", detail=str(exc)[:200])

    # One Slack account → one Engineers user (clear stale links).
    others = await UserExtra.find(
        {"slackUserId": slack_uid, "_id": {"$ne": user.id}}
    ).to_list()
    for other in others:
        other.slackUserId = None
        await other.save()

    user.slackUserId = slack_uid
    user.slackAlertsEnabled = True
    await user.save()
    return _frontend_redirect(status_value="connected")


@router.delete("/disconnect")
async def slack_disconnect(user: Annotated[UserExtra, Depends(get_current_user)]) -> dict:
    user.slackUserId = None
    await user.save()
    return _slack_profile_fields(user)


@router.patch("/prefs")
async def slack_prefs(
    body: SlackPrefsUpdate,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    if body.slackAlertsEnabled is not None:
        user.slackAlertsEnabled = body.slackAlertsEnabled
    if body.slackDigestHour is not None:
        user.slackDigestHour = body.slackDigestHour
    if body.slackDigestMinute is not None:
        user.slackDigestMinute = body.slackDigestMinute
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
            detail="Connect Slack before sending a test DM",
        )
    ok = await send_dm(
        slack_id,
        text="Engineers bot is connected. You'll get interview reminders here.",
        blocks=[
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        "*Engineers bot connected*\n"
                        "You'll get a DM ~30 minutes before each interview "
                        "and a morning digest of today's interviews."
                    ),
                },
            }
        ],
    )
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Failed to send Slack DM — open a chat with the bot once and retry",
        )
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
