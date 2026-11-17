"""Where a Friday result roundup was posted in Slack, so a later answer can edit it in place."""

from pymongo import IndexModel

from app.models.base import TimestampedDocument


class ResultRoundupPost(TimestampedDocument):
    friday: str  # YYYY-MM-DD, the Friday's date in America/Chicago
    channel: str
    ts: str
    refs: list[str]  # "<interviewId>:<roundId>" of every round listed when posted (also the ones left to "…and k more")
    rendered: str  # sha1 of json.dumps(blocks, sort_keys=True): an unchanged render makes no chat.update

    class Settings:
        name = "resultroundupposts"
        indexes = [IndexModel([("friday", 1)], unique=True)]
