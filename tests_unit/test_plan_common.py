"""Unit tests for the plan count models (self bids split by region)."""

import pytest
from pydantic import ValidationError

from app.models.plan_common import BidsIn, Counts, CountsIn, bids_of


def test_typed_regions_set_the_self_total():
    bids = BidsIn(bidsSelf=999, bidsSelfUs=3, bidsSelfEu=2, bidsSelfLatam=1, bidsBidder=4)
    assert (bids.bidsSelf, bids.bidsBidder) == (6, 4)
    assert CountsIn(bidsSelfLatam=5, interviewsSelf=2).bidsSelf == 5


def test_pre_split_total_is_kept_without_regions():
    assert BidsIn(bidsSelf=7).bidsSelf == 7


def test_stored_counts_are_not_recomputed():
    # A week mixing pre-split days and split days: the total is more than the regions.
    assert Counts(bidsSelf=11, bidsSelfUs=4).bidsSelf == 11


def test_regions_cannot_be_negative():
    with pytest.raises(ValidationError):
        BidsIn(bidsSelfEu=-1)


def test_bids_of_copies_every_bid_field():
    c = Counts(bidsSelf=6, bidsSelfUs=1, bidsSelfEu=2, bidsSelfLatam=3, bidsBidder=4, interviewsSelf=9)
    assert Counts(**bids_of(c)) == Counts(bidsSelf=6, bidsSelfUs=1, bidsSelfEu=2, bidsSelfLatam=3, bidsBidder=4)


def test_all_zero_profile_map_still_sums_regions():
    assert BidsIn(bidsSelfByProfile={"a": 0}, bidsSelfUs=3).bidsSelf == 3
    assert BidsIn(bidsSelfByProfile={"a": 5}, bidsSelf=9, bidsSelfUs=5).bidsSelf == 9  # put_day derives both


def test_plan_item_defaults_and_limits():
    from app.models.plan_common import PlanItem

    a, b = PlanItem(text="  Read  "), PlanItem(text="x")
    assert a.text == "Read" and len(a.id) == 12 and a.id != b.id
    assert (a.kind, a.target, a.archived) == ("check", None, False)
    with pytest.raises(ValidationError):
        PlanItem(text="x" * 201)
    with pytest.raises(ValidationError):
        PlanItem(text="   ")
    with pytest.raises(ValidationError):
        PlanItem(text="x", kind="count", target=-1)


@pytest.fixture
async def _db():
    from beanie import init_beanie
    from mongomock_motor import AsyncMongoMockClient

    from app.models import DailyPlan, WeeklyPlan

    await init_beanie(database=AsyncMongoMockClient()["plan-common-unit"], document_models=[DailyPlan, WeeklyPlan])


@pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning")
async def test_daily_plan_new_fields_default_empty(_db):
    from datetime import datetime

    from app.models.daily_plan import DailyPlan
    from app.models.weekly_plan import WeeklyPlan

    d = DailyPlan(userId="64b000000000000000000001", date=datetime(2026, 1, 5), year=2026, weekNumber=2)
    assert (d.targetOverride, d.results, d.focus, d.submittedAt) == (None, {}, [], None)
    w = WeeklyPlan(
        userId="64b000000000000000000001", weekStart=datetime(2026, 1, 5), weekNumber=2, year=2026,
        startDate=datetime(2026, 1, 5), endDate=datetime(2026, 1, 11),
    )
    assert (w.routines, w.weeklyGoals) == ([], [])


@pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning")
async def test_legacy_docs_still_load(_db):
    from app.models.daily_plan import DailyPlan
    from app.models.weekly_plan import WeeklyPlan

    d = DailyPlan.model_validate({
        "userId": "64b000000000000000000001", "date": "2026-01-05T00:00:00", "year": 2026, "weekNumber": 2,
        "goal": {"bidsSelf": 3}, "goalItems": [{"text": "a", "done": True}], "done": {"bidsSelf": 2},
    })
    assert d.goal.bidsSelf == 3 and d.results == {}
    w = WeeklyPlan.model_validate({
        "userId": "64b000000000000000000001", "weekStart": "2026-01-05T00:00:00", "weekNumber": 2, "year": 2026,
        "startDate": "2026-01-05T00:00:00", "endDate": "2026-01-11T00:00:00",
        "dailyGoal": {"bidsSelf": 5}, "dailyGoalItems": [{"text": "b"}],
    })
    assert w.dailyGoal.bidsSelf == 5 and w.routines == []


from app.models.plan_common import ItemResult, LogEntry, PlanItem, result_is_done  # noqa: E402


def test_log_kind_and_entries():
    assert PlanItem(text="Profile review", kind="log").kind == "log"
    r = ItemResult(entries=[{"profileId": "a" * 24, "note": " rewrote summary "}])
    assert r.entries[0].note == "rewrote summary"
    assert ItemResult().entries == []  # old documents read fine


def test_log_entry_limits():
    with pytest.raises(ValueError):
        LogEntry(note="   ")
    with pytest.raises(ValueError):
        LogEntry(note="x" * 201)
    with pytest.raises(ValueError):
        ItemResult(entries=[{"note": "n"}] * 21)


def test_result_is_done():
    assert result_is_done("log", ItemResult(entries=[{"note": "n"}]), None)
    assert not result_is_done("log", ItemResult(done=True), None)  # a tick without entries is not done
    assert not result_is_done("log", None, None)
    assert result_is_done("check", ItemResult(done=True), None)
    assert result_is_done("count", ItemResult(value=50), 50)
    assert not result_is_done("count", ItemResult(value=49), 50)
    assert result_is_done("hours", ItemResult(done=True, value=1), 2)  # ticked short


def test_choice_options_validated_and_stripped():
    it = PlanItem(text="Status", kind="choice", options=[" Done ", "Blocked"], target=3)
    assert it.options == ["Done", "Blocked"] and it.target is None
    for bad in (["Only"], [str(i) for i in range(7)], ["A", "a"], ["A", " "], ["A", "x" * 41], []):
        with pytest.raises(ValueError):
            PlanItem(text="Status", kind="choice", options=bad)
    assert len(PlanItem(text="s", kind="choice", options=list("abcdef")).options) == 6


def test_other_kinds_force_options_empty():
    assert PlanItem(text="x", kind="text", options=["a", "b"]).options == []
    assert PlanItem(text="x", options=["a"]).options == []
    assert PlanItem(text="x").options == []


def test_text_and_choice_results():
    assert ItemResult(text="  hi  ").text == "hi"
    assert ItemResult(text="   ").text is None and ItemResult().choice is None
    with pytest.raises(ValueError):
        ItemResult(text="x" * 501)
    assert result_is_done("text", ItemResult(text="a"), None)
    assert not result_is_done("text", ItemResult(done=True), None)
    assert not result_is_done("text", None, None)
    assert result_is_done("choice", ItemResult(choice="Done"), None)
    assert not result_is_done("choice", ItemResult(text="note only"), None)


def test_plan_item_days_normalised():
    assert PlanItem(text="R").days is None
    assert PlanItem(text="R", days=[4, 0, 4]).days == [0, 4]
    assert PlanItem(text="R", days=[5, 4, 3, 2, 1, 0]).days is None  # every working day
    with pytest.raises(ValidationError, match="Pick at least one day"):
        PlanItem(text="R", days=[])
    for bad in ([6], [-1, 2]):
        with pytest.raises(ValidationError):
            PlanItem(text="R", days=bad)
