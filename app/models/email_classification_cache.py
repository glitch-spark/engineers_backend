"""Template hash → classifier verdict cache.

Identical templates (every Greenhouse "application received") hash the
same. Subsequent messages with the same hash short-circuit the LLM call.

Hash key combines sender domain + normalized subject + first 200 body
chars. Body changes per-recipient (name, role), but the boilerplate
front matter is template-stable enough for ~30% hit rate in practice.

TTL is enforced at read time (not via Mongo TTL index) so we can keep
hot entries refreshed via hitCount without expiry surprises.
"""

from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel
from pymongo import IndexModel

from app.models.base import TimestampedDocument


class EmailClassificationCache(TimestampedDocument):
    templateHash: str            # sha256 hex
    label: str
    confidence: float
    stageRound: Optional[str] = None
    companyHint: Optional[str] = None
    evidenceQuote: Optional[str] = None
    senderDomain: str
    subjectPreview: str          # for debug
    hitCount: int = 1
    lastHitAt: Optional[datetime] = None

    class Settings:
        name = "emailClassificationCache"
        indexes = [
            IndexModel([("templateHash", 1)], unique=True),
            IndexModel([("senderDomain", 1)]),
        ]
