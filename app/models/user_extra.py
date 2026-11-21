from datetime import datetime
from typing import Literal, Optional

from pymongo import IndexModel

from app.models.base import TimestampedDocument

# interview_manager: staff access, can view any interview, tagged on every #caller interview post.
UserRole = Literal["admin", "staff", "interview_manager"]


class UserExtra(TimestampedDocument):
    name: Optional[str] = None
    email: Optional[str] = None
    hashedPassword: Optional[str] = None
    role: UserRole = "staff"
    phone: Optional[str] = None
    birthday: Optional[datetime] = None
    image: Optional[str] = None
    # New users wait for admin approval before they can log in. Default True so
    # pre-existing rows (no field set) treat as active — only the register flow
    # explicitly sets False on new sign-ups (after the very first user, who
    # bootstraps as admin).
    isActive: bool = True
    # Set on each successful /auth/login (UTC). None until the first login after this field shipped.
    lastLoginAt: Optional[datetime] = None

    # Opt-in to public leaderboard (Phase 1 — name + value visible to all
    # opted-in users). leaderboardName overrides display name on the
    # leaderboard only; leaderboardAnon hides identity behind a stable
    # pseudonym derived from userId.
    leaderboardOptIn: bool = False
    leaderboardName: Optional[str] = None
    leaderboardAnon: bool = False

    # User-level custom prompts. Empty → built-in default. Account-level
    # overrides take precedence per profile.
    resumePromptBody: Optional[str] = None
    screeningPromptBody: Optional[str] = None
    # Google Sheet that Job Applies exports go to (shared with the service account as Editor).
    jobApplySheetUrl: Optional[str] = None
    coverLetterPromptBody: Optional[str] = None

    # Slack interview bot. slackUserId is the member ID the user types in (U…): DMs and @-mentions.
    slackUserId: Optional[str] = None
    slackAlertsEnabled: bool = True
    slackTimezone: Optional[str] = None  # IANA, e.g. America/New_York
    slackDigestHour: Optional[int] = None
    slackDigestMinute: Optional[int] = None
    # False: the admin plans this person's week (Plans page setup grid)
    plansOwnWeek: bool = False

    # Calendar colour when this user is taken as a caller (None = a stable colour derived from the id).
    callerColor: Optional[str] = None

    class Settings:
        name = "userextras"
        indexes = [
            IndexModel([("email", 1)], unique=True),
        ]
