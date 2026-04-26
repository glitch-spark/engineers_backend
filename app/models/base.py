from datetime import datetime

from beanie import Document, Replace, SaveChanges, Update, before_event
from pydantic import Field


class TimestampedDocument(Document):
    """Mirrors Mongoose `{ timestamps: true }`; BSON keys stay camelCase so existing data and the SPA see the same shape."""

    createdAt: datetime = Field(default_factory=datetime.utcnow)
    updatedAt: datetime = Field(default_factory=datetime.utcnow)

    @before_event(Update, Replace, SaveChanges)
    def _bump_updated_at(self) -> None:
        self.updatedAt = datetime.utcnow()
