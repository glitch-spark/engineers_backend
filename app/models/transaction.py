from datetime import datetime
from typing import Literal, Optional

from beanie import PydanticObjectId

from app.models.base import TimestampedDocument

TransactionStatus = Literal["pending", "approved", "rejected"]
# 'crypto' is the legacy spelling kept here so Beanie can deserialize old rows.
# Drop it from the literal after running scripts/rename_paymethod_crypto_to_coin.
PayMethod = Literal["coin", "card", "crypto"]
BillingCycle = Literal["one_time", "monthly"]


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
    # Optional nickname for the card (may duplicate across last-4s).
    cardLabel: Optional[str] = None
    # Recurring vs one-off. Default monthly so historical rows and older
    # clients keep the previous "everything is a monthly card charge" meaning.
    billingCycle: Optional[BillingCycle] = "monthly"

    class Settings:
        name = "transactions"
