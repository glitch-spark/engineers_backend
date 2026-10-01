"""Goal vs Done building blocks shared by Daily and Weekly plans.

Plans track bids (self, bidder) and interviews (self, caller). Goals are
typed; bids Done is typed per day; interviews Done always comes from the
Interviews page (`app.plan_auto`).
"""

from datetime import date, timedelta
from typing import Annotated

from pydantic import AfterValidator, BaseModel, Field, field_validator

MAX_ITEMS = 20
MAX_ITEM_LENGTH = 200

Count = Annotated[int, Field(ge=0)]


class Counts(BaseModel):
    bidsSelf: Count = 0
    bidsBidder: Count = 0
    interviewsSelf: Count = 0
    interviewsCaller: Count = 0


class BidsIn(BaseModel):
    """Bids typed on a day (goal in the morning, Done at follow-up)."""

    bidsSelf: Count = 0
    bidsBidder: Count = 0


class ChecklistItem(BaseModel):
    text: str
    done: bool = False

    @field_validator("text")
    @classmethod
    def _text(cls, v: str) -> str:
        v = (v or "").strip()
        if not v:
            raise ValueError("checklist items need text")
        if len(v) > MAX_ITEM_LENGTH:
            raise ValueError(f"checklist items are limited to {MAX_ITEM_LENGTH} characters")
        return v


def check_items(items: list) -> list:
    if len(items) > MAX_ITEMS:
        raise ValueError(f"at most {MAX_ITEMS} checklist items")
    return items


ChecklistItems = Annotated[list[ChecklistItem], AfterValidator(check_items)]


def next_working_day(d: date) -> date:
    """The next Monday–Saturday day after `d` (Saturday and Sunday → Monday)."""
    nxt = d + timedelta(days=1)
    return nxt + timedelta(days=1) if nxt.weekday() == 6 else nxt


def monday_of(d: date) -> date:
    """Monday of the Mon–Sun week holding `d` (Sunday belongs to the week that just ended)."""
    return d - timedelta(days=d.weekday())
