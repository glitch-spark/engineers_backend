"""Caller teams: rounds assigned to a team are posted to the callers' Slack workspace."""

from datetime import datetime
from typing import Optional

from pymongo import IndexModel

from app.models.base import TimestampedDocument


class CallerTeam(TimestampedDocument):
    name: str
    nameKey: str  # lowercased name: one team per name
    managerCallerId: str
    channelId: str
    reminderChannelId: str
    dailyTime: str = "06:00"
    timezone: str = "America/Chicago"
    archivedAt: Optional[datetime] = None

    class Settings:
        name = "callerteams"
        indexes = [IndexModel([("nameKey", 1)], unique=True)]


class TeamRoundPost(TimestampedDocument):
    teamId: str
    interviewId: str
    roundId: str
    channel: str
    ts: str = ""
    callerId: str = ""
    startEpoch: Optional[int] = None
    filesFor: Optional[int] = None  # -1 = files sent for a TBD round
    state: str  # "scheduled" | "canceled" | "off"

    class Settings:
        name = "teamroundposts"
        indexes = [IndexModel([("teamId", 1), ("roundId", 1)], unique=True)]


class TeamDayPost(TimestampedDocument):
    teamId: str
    day: str  # YYYY-MM-DD in the team's timezone
    channel: str
    ts: list[str]
    interviewIds: list[str] = []

    class Settings:
        name = "teamdayposts"
        indexes = [IndexModel([("teamId", 1), ("day", 1)], unique=True)]
