"""Goal vs Done building blocks shared by Daily and Weekly plans.

Plans track bids (self, bidder) and interviews (self, caller). Self bids are
split by region (US, EU, LATAM); `bidsSelf` stays the self total, so plans
from before the split (total only) still add up. Goals are typed; bids Done
is typed per day; interviews Done always comes from the Interviews page
(`app.plan_auto`).
"""

from datetime import date, timedelta
from typing import Annotated, Dict, Literal, Optional
from uuid import uuid4

from pydantic import AfterValidator, BaseModel, Field, field_validator, model_validator

MAX_ITEMS = 20
MAX_ITEM_LENGTH = 200
MAX_LOG_ENTRIES = 20
MAX_ANSWER_LENGTH = 500  # a Text answer or a Choice note
MAX_OPTION_LENGTH = 40

Count = Annotated[int, Field(ge=0)]


SELF_REGIONS = ("bidsSelfUs", "bidsSelfEu", "bidsSelfLatam")
BID_FIELDS = ("bidsSelf", *SELF_REGIONS, "bidsBidder")


class Bids(BaseModel):
    bidsSelf: Count = 0  # self total: regions plus other-region profiles (Canada, Asia, none), or a pre-split total
    bidsSelfUs: Count = 0
    bidsSelfEu: Count = 0
    bidsSelfLatam: Count = 0
    bidsBidder: Count = 0


class Counts(Bids):
    interviewsSelf: Count = 0
    interviewsCaller: Count = 0


class DoneCounts(Counts):
    """A day's Done: self bids typed per profile (accountId hex -> count) are kept beside the totals."""

    bidsSelfByProfile: Dict[str, Count] = {}
    # Profiles on tracked self bids (the extension): what was typed is "other applies" (not captured), kept here and
    # added to the tracked count in bidsSelfByProfile.
    bidsSelfOtherByProfile: Dict[str, Count] = {}


def bids_of(c: Bids) -> dict[str, int]:
    """The bid fields of `c`, to copy into another Counts."""
    return {f: getattr(c, f) for f in BID_FIELDS}


class _SelfTotal(BaseModel):
    @model_validator(mode="after")
    def self_total_from_regions(self):
        """Typed self bids: once any region is set, the total is their sum (per-profile saves derive both; an
        all-zero map is dropped on save, so it doesn't count as typed per profile)."""
        if any((getattr(self, "bidsSelfByProfile", None) or {}).values()):
            return self
        regions = sum(getattr(self, f) for f in SELF_REGIONS)
        if regions:
            self.bidsSelf = regions
        return self


class BidsIn(Bids, _SelfTotal):
    """Bids typed on a day (goal in the morning, Done at follow-up)."""

    bidsSelfByProfile: Dict[str, Count] = {}


class CountsIn(Counts, _SelfTotal):
    """A typed goal (bids and interviews)."""


class SatGoal(BaseModel):
    """A week's Saturday share of its bid goal; None = the even Mon-Sat split (`plan_auto.day_targets`)."""

    bidsSelf: Optional[Count] = None
    bidsBidder: Optional[Count] = None


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


class PlanItem(BaseModel):
    """A routine or weekly goal line; `target` is ignored for kind "check". `days`: the weekdays (0=Mon..5=Sat) a
    routine runs on, None = every working day; weekly goals are for the whole week (always None)."""

    id: str = Field(default_factory=lambda: uuid4().hex[:12])
    text: str
    kind: Literal["check", "count", "hours", "log", "text", "choice"] = "check"
    target: Optional[float] = Field(default=None, ge=0)
    options: list[str] = []  # choice only: 2-6 options (stripped, unique ignoring case); [] for every other kind
    archived: bool = False
    addedBy: Literal["admin", "self"] = "admin"  # who added the item; stored items without it are the admin's
    days: Optional[list[int]] = None

    @field_validator("text")
    @classmethod
    def _text(cls, v: str) -> str:
        return ChecklistItem._text(v)

    @field_validator("days")
    @classmethod
    def _days(cls, v: Optional[list[int]]) -> Optional[list[int]]:
        if v is None:
            return None
        v = sorted(set(v))
        if not v:
            raise ValueError("Pick at least one day")
        if v[0] < 0 or v[-1] > 5:
            raise ValueError("days are 0 (Mon) to 5 (Sat)")
        return None if len(v) == 6 else v

    @model_validator(mode="after")
    def _options(self):
        if self.kind != "choice":
            self.options = []
            return self
        opts = [o.strip() for o in self.options]
        if not 2 <= len(opts) <= 6:
            raise ValueError("a choice needs 2 to 6 options")
        if any(not 1 <= len(o) <= MAX_OPTION_LENGTH for o in opts):
            raise ValueError(f"options are 1 to {MAX_OPTION_LENGTH} characters")
        if len({o.lower() for o in opts}) != len(opts):
            raise ValueError("options must be unique")
        self.options, self.target = opts, None
        return self


class LogEntry(BaseModel):
    """One line of a log item's day result: what changed, optionally on a profile."""

    profileId: Optional[str] = None
    note: str

    @field_validator("note")
    @classmethod
    def _note(cls, v: str) -> str:
        return ChecklistItem._text(v)


def clean_answer(v: Optional[str]) -> Optional[str]:
    """A Text answer / Choice note: stripped, empty -> None, at most MAX_ANSWER_LENGTH characters."""
    v = (v or "").strip()
    if len(v) > MAX_ANSWER_LENGTH:
        raise ValueError(f"answers are limited to {MAX_ANSWER_LENGTH} characters")
    return v or None


class ItemResult(BaseModel):
    """A day's result for one plan item (by PlanItem.id): done for checks, value for counts/hours, entries for logs."""

    done: bool = False
    value: Optional[float] = Field(default=None, ge=0)
    target: Optional[float] = Field(default=None, ge=0)
    entries: list[LogEntry] = []
    text: Optional[str] = None  # Text answer, or a Choice's note
    choice: Optional[str] = None  # Choice: the picked option (kept even if the option is later removed)

    @field_validator("text")
    @classmethod
    def _answer(cls, v: Optional[str]) -> Optional[str]:
        return clean_answer(v)

    @field_validator("entries")
    @classmethod
    def _entries(cls, v: list) -> list:
        if len(v) > MAX_LOG_ENTRIES:
            raise ValueError(f"at most {MAX_LOG_ENTRIES} log entries")
        return v


def result_is_done(kind: str, r: Optional[ItemResult], target: Optional[float]) -> bool:
    """A day's result is done: log = has entries; text = an answer; choice = an option picked; otherwise ticked, or its value at / over `target`."""
    if r is None:
        return False
    if kind == "log":
        return bool(r.entries)
    if kind == "text":
        return bool(r.text)
    if kind == "choice":
        return r.choice is not None
    return bool(r.done or (r.value is not None and target is not None and r.value >= target))


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
