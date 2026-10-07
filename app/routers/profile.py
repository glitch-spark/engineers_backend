from datetime import datetime
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.deps import get_current_user
from app.config import get_settings
from app.free_llm import api_key_hint, clamp_max_tokens, get_preset, preset_catalog_public
from app.llm_router import test_free_connection
from app.models import UserExtra
from app.security import hash_password, verify_password
from app.slack_bot import (
    normalize_slack_timezone,
    slack_bot_configured,
)
from app.slack_timezones import slack_timezone_catalog

router = APIRouter(prefix="/profile", tags=["profile"])


class ProfileUpdate(BaseModel):
    username: str
    email: str
    image: Optional[str] = None
    birthday: Optional[str] = None
    leaderboardOptIn: Optional[bool] = None
    leaderboardName: Optional[str] = None
    leaderboardAnon: Optional[bool] = None
    resumePromptBody: Optional[str] = None
    screeningPromptBody: Optional[str] = None
    coverLetterPromptBody: Optional[str] = None
    freeLlmModelId: Optional[str] = None
    freeLlmMaxTokens: Optional[int] = None
    freeLlmApiKey: Optional[str] = None


class FreeLlmSettingsUpdate(BaseModel):
    freeLlmModelId: Optional[str] = None
    freeLlmMaxTokens: Optional[int] = Field(default=None, ge=512, le=16384)
    freeLlmApiKey: Optional[str] = None


class PasswordChange(BaseModel):
    currentPassword: str
    newPassword: str


def _slack_profile_fields(user: UserExtra) -> dict:
    s = get_settings()
    connected = bool((user.slackUserId or "").strip())
    tz_name = normalize_slack_timezone(
        (user.slackTimezone or "").strip()
        or (s.APP_TIMEZONE or "").strip()
        or "America/New_York"
    )
    return {
        "slackConnected": connected,
        "slackAlertsEnabled": bool(user.slackAlertsEnabled),
        "slackTimezone": tz_name,
        "slackTimezones": slack_timezone_catalog(),
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
        "slackBotConfigured": slack_bot_configured(),
    }


def _parse_dt(s: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _free_llm_profile_fields(user: UserExtra) -> dict:
    key_set = bool((user.freeLlmApiKey or "").strip())
    return {
        "freeLlmModelId": user.freeLlmModelId or "",
        "freeLlmMaxTokens": user.freeLlmMaxTokens,
        "freeLlmApiKeySet": key_set,
        "freeLlmApiKeyHint": api_key_hint(user.freeLlmApiKey),
        "freeLlmKeyVerified": bool(key_set and user.freeLlmKeyVerified),
    }


def _apply_free_llm_settings(user: UserExtra, body: FreeLlmSettingsUpdate) -> None:
    if body.freeLlmModelId is not None:
        mid = body.freeLlmModelId.strip()
        if mid and get_preset(mid) is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unknown free LLM model id: {mid}",
            )
        user.freeLlmModelId = mid or None
    if body.freeLlmMaxTokens is not None:
        preset = get_preset(user.freeLlmModelId)
        user.freeLlmMaxTokens = clamp_max_tokens(body.freeLlmMaxTokens, preset)
    if body.freeLlmApiKey is not None:
        key = body.freeLlmApiKey.strip()
        if key:
            if key != (user.freeLlmApiKey or "").strip():
                user.freeLlmKeyVerified = False
            user.freeLlmApiKey = key


@router.get("")
async def get_profile(user: Annotated[UserExtra, Depends(get_current_user)]) -> dict:
    return {
        "user": {
            "username": user.name or "",
            "email": user.email or "",
            "image": user.image or "",
            "birthday": user.birthday,
            "leaderboardOptIn": user.leaderboardOptIn,
            "leaderboardName": user.leaderboardName or "",
            "leaderboardAnon": user.leaderboardAnon,
            "resumePromptBody": user.resumePromptBody or "",
            "screeningPromptBody": user.screeningPromptBody or "",
            "coverLetterPromptBody": user.coverLetterPromptBody or "",
            **_free_llm_profile_fields(user),
            **_slack_profile_fields(user),
        }
    }


@router.get("/free-llm-models")
async def list_free_llm_models(
    _user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    return {"models": preset_catalog_public()}


@router.put("/free-llm")
async def update_free_llm_settings(
    body: FreeLlmSettingsUpdate,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    _apply_free_llm_settings(user, body)
    await user.save()
    return {
        "message": "Free LLM settings saved",
        **_free_llm_profile_fields(user),
    }


@router.post("/free-llm-test")
async def test_free_llm(
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    try:
        result = await test_free_connection(user)
    except ValueError as exc:
        user.freeLlmKeyVerified = False
        await user.save()
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except Exception as exc:
        user.freeLlmKeyVerified = False
        await user.save()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=str(exc) or "Free LLM test failed",
        ) from exc
    user.freeLlmKeyVerified = True
    await user.save()
    return {**result, **_free_llm_profile_fields(user)}


@router.put("")
async def update_profile(
    body: ProfileUpdate,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    if not body.username or not body.email:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Username and email are required")

    new_email = body.email.lower().strip()

    if new_email != (user.email or "").lower():
        existing = await UserExtra.find_one(UserExtra.email == new_email)
        if existing and str(existing.id) != str(user.id):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Email is already taken")

    user.name = body.username.strip()
    user.email = new_email
    user.image = body.image or ""
    if body.birthday:
        parsed = _parse_dt(body.birthday)
        if parsed is not None:
            user.birthday = parsed
    if body.leaderboardName is not None:
        user.leaderboardName = body.leaderboardName.strip() or None
    if body.leaderboardAnon is not None:
        user.leaderboardAnon = body.leaderboardAnon
    if body.resumePromptBody is not None:
        user.resumePromptBody = body.resumePromptBody[:30000] or None
    if body.screeningPromptBody is not None:
        user.screeningPromptBody = body.screeningPromptBody[:30000] or None
    if body.coverLetterPromptBody is not None:
        user.coverLetterPromptBody = body.coverLetterPromptBody[:30000] or None
    if (
        body.freeLlmModelId is not None
        or body.freeLlmMaxTokens is not None
        or body.freeLlmApiKey is not None
    ):
        _apply_free_llm_settings(
            user,
            FreeLlmSettingsUpdate(
                freeLlmModelId=body.freeLlmModelId,
                freeLlmMaxTokens=body.freeLlmMaxTokens,
                freeLlmApiKey=body.freeLlmApiKey,
            ),
        )
    await user.save()

    return {
        "message": "Profile updated successfully",
        "user": {
            "username": user.name,
            "email": user.email,
            "image": user.image,
            "birthday": user.birthday,
            "leaderboardOptIn": user.leaderboardOptIn,
            "leaderboardName": user.leaderboardName or "",
            "leaderboardAnon": user.leaderboardAnon,
            "resumePromptBody": user.resumePromptBody or "",
            "screeningPromptBody": user.screeningPromptBody or "",
            **_free_llm_profile_fields(user),
        },
    }


@router.put("/password")
async def change_password(
    body: PasswordChange,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    if not body.currentPassword or not body.newPassword:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Current password and new password are required",
        )
    if len(body.newPassword) < 6:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="New password must be at least 6 characters long",
        )
    if not user.hashedPassword:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Current password verification failed")

    if not verify_password(body.currentPassword, user.hashedPassword):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Current password is incorrect")

    user.hashedPassword = hash_password(body.newPassword)
    await user.save()
    return {"message": "Password changed successfully"}
