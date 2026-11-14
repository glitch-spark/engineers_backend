"""Where the day's interview post went in Slack, so a later change can edit it in place."""

from pymongo import IndexModel

from app.models.base import TimestampedDocument


class InterviewDayPost(TimestampedDocument):
    day: str  # YYYY-MM-DD, the local day in the daily channel's timezone
    channel: str
    ts: list[str]  # ts[0] is the post, the rest its overflow messages (a message holds at most 50 blocks)

    class Settings:
        name = "interviewdayposts"
        indexes = [IndexModel([("day", 1)], unique=True)]
