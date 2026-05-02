from typing import Optional

from beanie import PydanticObjectId
from pymongo import IndexModel

from app.models.base import TimestampedDocument


class ResumeApplication(TimestampedDocument):
    """One generated resume — `(userId, accountId, companyNameLower)` is unique
    to prevent re-applying to the same company with the same account by accident.
    """

    userId: PydanticObjectId
    accountId: PydanticObjectId
    companyName: str
    companyNameLower: str
    jobDescription: str
    s3Key: Optional[str] = None
    s3Url: Optional[str] = None

    class Settings:
        name = "resumeapplications"
        indexes = [
            IndexModel(
                [("userId", 1), ("accountId", 1), ("companyNameLower", 1)],
                unique=True,
                name="uniq_user_account_company",
            ),
        ]
