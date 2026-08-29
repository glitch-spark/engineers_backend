from datetime import datetime
from typing import Literal, Optional

from beanie import PydanticObjectId
from pymongo import IndexModel

from app.models.base import TimestampedDocument

RecurringChargeStatus = Literal["active", "stopped"]


class RecurringCharge(TimestampedDocument):
    """Monthly card subscription inferred from ledger rows.

    Identity is (userId, cardLast4, title). Several charges can share one
    last-4; cardLabel is display-only and may duplicate.
    """

    userId: PydanticObjectId
    title: str
    cardLast4: str
    cardLabel: Optional[str] = None
    amount: float = 0.0
    billingDay: int = 1
    status: RecurringChargeStatus = "active"
    lastPaidOn: Optional[datetime] = None
    sourceTransactionId: Optional[PydanticObjectId] = None
    ackedPeriod: Optional[str] = None

    class Settings:
        name = "recurringcharges"
        indexes = [
            IndexModel(
                [("userId", 1), ("cardLast4", 1), ("title", 1)],
                unique=True,
                name="uniq_charge",
            ),
            IndexModel([("status", 1), ("userId", 1)]),
        ]
