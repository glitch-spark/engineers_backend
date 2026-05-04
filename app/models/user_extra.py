from datetime import datetime
from typing import Literal, Optional

from pymongo import IndexModel

from app.models.base import TimestampedDocument

UserRole = Literal["admin", "staff", "accountant"]


class UserExtra(TimestampedDocument):
    name: Optional[str] = None
    email: Optional[str] = None
    hashedPassword: Optional[str] = None
    role: UserRole = "staff"
    phone: Optional[str] = None
    birthday: Optional[datetime] = None
    image: Optional[str] = None
    # New users wait for admin approval before they can log in. Default True so
    # pre-existing rows (no field set) treat as active — only the register flow
    # explicitly sets False on new sign-ups (after the very first user, who
    # bootstraps as admin).
    isActive: bool = True

    class Settings:
        name = "userextras"
        indexes = [
            IndexModel([("email", 1)], unique=True),
        ]
