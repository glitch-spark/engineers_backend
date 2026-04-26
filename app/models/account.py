from typing import Optional

from beanie import PydanticObjectId

from app.models.base import TimestampedDocument


class Account(TimestampedDocument):
    name: str
    email: str
    phone: Optional[str] = None
    address: Optional[str] = None
    createdBy: PydanticObjectId

    class Settings:
        name = "accounts"
