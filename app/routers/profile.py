from datetime import datetime
from typing import Annotated, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel

from app.bids.schemas import password_problem
from app.caller_directory import user_display_name
from app.deps import get_current_user
from app.config import get_settings
from app.models import UserExtra
from app.resume_prompts import max_body
from app.routers.callers import rename_on_rounds
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
        "slackTimezone": tz_name,
        "slackTimezones": slack_timezone_catalog(),
        "slackBotConfigured": slack_bot_configured(),
    }


def _parse_dt(s: str) -> Optional[datetime]:
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


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
            **_slack_profile_fields(user),
        }
    }


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

    old_caller_name = user_display_name(user)
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
        user.resumePromptBody = body.resumePromptBody[:max_body()] or None
    if body.screeningPromptBody is not None:
        user.screeningPromptBody = body.screeningPromptBody[:30000] or None
    if body.coverLetterPromptBody is not None:
        user.coverLetterPromptBody = body.coverLetterPromptBody[:30000] or None
    await user.save()
    if user_display_name(user) != old_caller_name:
        await rename_on_rounds(str(user.id), user_display_name(user))

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
    problem = password_problem(body.newPassword)
    if problem:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=problem)
    if not user.hashedPassword:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Current password verification failed")

    try:
        current_ok = verify_password(body.currentPassword, user.hashedPassword)
    except ValueError:  # admin-created users hold a non-hash placeholder
        current_ok = False
    if not current_ok:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Current password is incorrect")

    user.hashedPassword = hash_password(body.newPassword)
    await user.save()
    return {"message": "Password changed successfully"}
