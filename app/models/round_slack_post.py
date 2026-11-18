"""A round's home message in the daily channel: its 30-minute reminder (or, without one, its first follow-up). Every
later follow-up for that round and start replies in its thread."""

from pymongo import IndexModel

from app.models.base import TimestampedDocument


class RoundSlackPost(TimestampedDocument):
    roundId: str
    interviewId: str
    startEpoch: int  # the round's start (UTC seconds) the message was for: a rescheduled round gets a new home
    channel: str
    ts: str

    class Settings:
        name = "roundslackposts"
        indexes = [IndexModel([("roundId", 1), ("startEpoch", 1)], unique=True)]
