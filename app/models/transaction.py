from datetime import datetime
from typing import Literal, Optional

from beanie import PydanticObjectId

from app.models.base import TimestampedDocument

TransactionStatus = Literal["pending", "approved", "rejected"]


class Transaction(TimestampedDocument):
    userId: PydanticObjectId
    date: datetime
    amount: float
    description: Optional[str] = None
    notes: Optional[str] = None
    status: TransactionStatus = "pending"
    approvedBy: Optional[PydanticObjectId] = None
    approvedAt: Optional[datetime] = None

    class Settings:
        name = "transactions"
