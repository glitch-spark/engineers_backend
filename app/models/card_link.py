from datetime import datetime
from typing import Literal, Optional

from beanie import PydanticObjectId
from pydantic import ConfigDict, Field

from app.models.base import TimestampedDocument

CardLinkStatus = Literal["billing", "canceled"]


class CardLink(TimestampedDocument):
    model_config = ConfigDict(populate_by_name=True)

    userId: PydanticObjectId
    email: str
    cardNumber: str
    site: str
    from_: datetime = Field(alias="from")
    to: datetime
    status: CardLinkStatus = "billing"
    approvedBy: Optional[PydanticObjectId] = None
    approvedAt: Optional[datetime] = None

    class Settings:
        name = "cardlinks"
