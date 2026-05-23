from datetime import datetime
from typing import Literal, Optional

from beanie import PydanticObjectId

from app.models.base import TimestampedDocument

TransactionStatus = Literal["pending", "approved", "rejected"]
PayMethod = Literal["crypto", "card"]


class Transaction(TimestampedDocument):
    userId: PydanticObjectId
    date: datetime
    amount: float
    description: Optional[str] = None
    notes: Optional[str] = None
    status: TransactionStatus = "pending"
    approvedBy: Optional[PydanticObjectId] = None
    approvedAt: Optional[datetime] = None
    # Payment method. Required on new creates (enforced at the router);
    # nullable on the model so pre-migration rows still load.
    payMethod: Optional[PayMethod] = None
    # Last 4 digits of the card number when payMethod == "card". Validated
    # as exactly 4 digits at the router.
    cardLast4: Optional[str] = None

    class Settings:
        name = "transactions"
