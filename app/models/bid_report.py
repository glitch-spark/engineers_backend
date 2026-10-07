"""Per-bidder, per-period bid counts posted to Slack."""

from datetime import datetime
from typing import Optional

from beanie import PydanticObjectId
from pydantic import BaseModel
from pymongo import IndexModel

from app.bids.windows import Kind
from app.models.base import TimestampedDocument


class BidReportRow(BaseModel):
    """Plain row; core report logic uses it so it needs no database."""

    kind: Kind
    periodKey: str  # local date (report timezone) of the window's end
    periodStart: datetime  # UTC, [start, end)
    periodEnd: datetime
    bidderId: PydanticObjectId
    ownerId: PydanticObjectId
    bidderName: str  # snapshots from report time
    country: Optional[str] = None
    profileName: Optional[str] = None
    rate: float
    count: Optional[int] = None  # None when the folder could not be read
    amount: Optional[float] = None
    error: Optional[str] = None
    # Bid-record rows only (count is then the approved bids); None on rows counted from folders.
    pending: Optional[int] = None
    rejected: Optional[int] = None


class BidReport(TimestampedDocument, BidReportRow):
    class Settings:
        name = "bidreports"
        # Beanie merges declared indexes by their field-name set, so two indexes on the same three
        # fields collapse into one. The unique index also serves newest-first history (backward scan);
        # (kind, periodKey) serves load_rows.
        indexes = [
            IndexModel([("bidderId", 1), ("kind", 1), ("periodKey", 1)], unique=True),
            IndexModel([("kind", 1), ("periodKey", 1)]),
        ]
