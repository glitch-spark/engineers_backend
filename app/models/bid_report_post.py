"""Where a daily or weekly bid report was posted in Slack, so a later review can edit it in place."""

from pymongo import IndexModel

from app.bids.windows import Kind
from app.models.base import TimestampedDocument


class BidReportPost(TimestampedDocument):
    kind: Kind
    periodKey: str
    channel: str
    ts: list[str]  # ts[0] is the channel post, the rest its overflow replies (over 50 blocks)
    # A review that recounts a row of this period bumps rev and sets dirty; the cron edits the post and clears
    # dirty only if rev is still the one it rendered, so a review landing mid-edit is picked up next tick.
    rev: int = 0
    dirty: bool = False

    class Settings:
        name = "bidreportposts"
        indexes = [
            IndexModel([("kind", 1), ("periodKey", 1)], unique=True),
            IndexModel([("dirty", 1)]),
        ]
