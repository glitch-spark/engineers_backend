"""DB-backed email classification rule.

Sender pattern + phrase pattern → deterministic label. Both seed rows
(ATS templates we ship with the system) and learned rows (written when a
user confirms a Needs-Review email) share this collection — same code
path, no parallel "ATS library" vs "learned rules" split.

A match is sender-pattern AND phrase-pattern. Both are stored as raw
strings; matching treats `senderPattern` as a substring on the lowercase
sender email, and `phrasePattern` as a case-insensitive substring on
subject + first 2K of body. We deliberately avoid stored regex — eval
ergonomics + cheaper to admin-edit + no ReDoS risk.

Identical (sender, phrase) gets merged via the unique compound index;
re-confirming an existing rule just bumps `hitCount`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal, Optional

from beanie import PydanticObjectId
from pydantic import BaseModel
from pymongo import IndexModel

from app.models.base import TimestampedDocument


RuleSource = Literal["seed", "user_correction", "admin_added"]


class EmailRule(TimestampedDocument):
    senderPattern: str           # lowercase substring on sender email (e.g. "@greenhouse-mail.com")
    phrasePattern: str           # case-insensitive substring on subject+body
    label: str                   # one of EmailLabel
    stageRound: Optional[str] = None  # target KanbanStage when applicable
    confidence: float = 1.0      # rules default to 1.0; admin can lower for fuzzy matches
    source: RuleSource = "seed"
    createdBy: Optional[PydanticObjectId] = None
    hitCount: int = 0
    lastHitAt: Optional[datetime] = None
    enabled: bool = True
    notes: str = ""

    class Settings:
        name = "emailRules"
        indexes = [
            IndexModel(
                [("senderPattern", 1), ("phrasePattern", 1)],
                unique=True,
                name="uniq_sender_phrase",
            ),
            IndexModel([("enabled", 1)]),
            IndexModel([("label", 1)]),
        ]


# ----- API DTO -----


class EmailRulePublic(BaseModel):
    id: str
    senderPattern: str
    phrasePattern: str
    label: str
    stageRound: Optional[str] = None
    confidence: float
    source: RuleSource
    hitCount: int
    lastHitAt: Optional[datetime] = None
    enabled: bool
    notes: str
    createdAt: datetime

    @classmethod
    def from_doc(cls, doc: "EmailRule") -> "EmailRulePublic":
        return cls(
            id=str(doc.id),
            senderPattern=doc.senderPattern,
            phrasePattern=doc.phrasePattern,
            label=doc.label,
            stageRound=doc.stageRound,
            confidence=doc.confidence,
            source=doc.source,
            hitCount=doc.hitCount,
            lastHitAt=doc.lastHitAt,
            enabled=doc.enabled,
            notes=doc.notes,
            createdAt=doc.createdAt,
        )
