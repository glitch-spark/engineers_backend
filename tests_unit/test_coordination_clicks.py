"""Choose caller and Confirm from the coordination post. Each click calls the router function the app calls; the post
re-renders through `sync_caller_thread` (spied). Slack is never reached: `_post_ephemeral` is patched."""

import os
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

import pytest  # noqa: E402
from beanie import PydanticObjectId, init_beanie  # noqa: E402
from mongomock_motor import AsyncMongoMockClient  # noqa: E402

from app.coordination import actions  # noqa: E402
from app.coordination.actions import CHOOSE_REFUSED, ROUND_GONE, Click  # noqa: E402
from app.models import Account, UserExtra  # noqa: E402
from app.models.caller import Caller  # noqa: E402
from app.models.interview import Interview, InterviewCaller, InterviewStageEvent  # noqa: E402
from app.models.interview_day_post import InterviewDayPost  # noqa: E402
from app.models.interview_question import InterviewQuestion  # noqa: E402
from app.routers import interviews  # noqa: E402

pytestmark = [
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]

START = datetime(2026, 10, 7, 15, 0, tzinfo=timezone.utc)
RESPONSE_URL = "https://hooks.slack.com/actions/T1/1/abc"
LINK = "https://meet.x/abc"
CONFIRM_REFUSAL = "Only the caller or an interview manager can confirm."
SELF_REFUSAL = "You take your own interview as Self — pick someone else as the caller."


@pytest.fixture
async def db():
    database = AsyncMongoMockClient()["coord-clicks-unit"]
    await init_beanie(
        database=database,
        document_models=[Interview, UserExtra, Account, InterviewQuestion, InterviewDayPost, Caller],
    )
    return database


class Replies(list):
    sync: AsyncMock


@pytest.fixture
def slack(db, monkeypatch):
    sync = AsyncMock(return_value=False)
    monkeypatch.setattr(interviews, "sync_caller_thread", sync)
    monkeypatch.setattr(interviews, "refresh_today", AsyncMock(return_value=False))
    replies = Replies()
    replies.sync = sync

    async def respond(url, payload):
        replies.append((url, payload))

    with patch.object(actions, "_post_ephemeral", respond):
        yield replies


class Spies:
    pass


@pytest.fixture
def spies(monkeypatch):
    spy = Spies()
    spy.update = AsyncMock(wraps=interviews.update_interview_stage)
    spy.assign = AsyncMock(wraps=interviews.assign_round_caller)
    monkeypatch.setattr(interviews, "update_interview_stage", spy.update)
    monkeypatch.setattr(interviews, "assign_round_caller", spy.assign)
    return spy


async def make_user(name, role="staff", slack_id=None) -> UserExtra:
    return await UserExtra(name=name, email=f"{name.lower()}@x.io", role=role, slackUserId=slack_id).insert()


async def make_interview(owner, *, caller_id=None, kind="external", name="TBD", enabled=True) -> Interview:
    caller = InterviewCaller(
        enabled=enabled, callerId=caller_id, callerName=name, callerKind=kind, startsAt=START,
        method="video", methodValue=LINK,
    )
    entry = InterviewStageEvent(
        stage="intro", at=START, scheduledAt=START, endsAt=START.replace(hour=16), status="scheduled", caller=caller,
    )
    return await Interview(
        accountId=PydanticObjectId(), createdBy=owner.id, scheduledAt=START, companyName="Acme",
        stageHistory=[entry.model_dump()], stage="intro", status="scheduled",
    ).insert()


def click(action_id, iv, who, pick=None) -> Click:
    ref = f"{iv.id}:{iv.stageHistory[-1].id}"
    value = ref if pick is None else f"{ref}:{pick}"
    return Click(action_id, value, who, RESPONSE_URL, "T.1", "C1", "1.2")


async def tip(iv) -> InterviewStageEvent:
    return (await Interview.get(iv.id)).stageHistory[-1]


def texts(replies) -> list[str]:
    return [p["text"] for _, p in replies]


@pytest.fixture
async def cast(db):
    class Cast:
        pass

    c = Cast()
    c.justin = await make_user("Justin", slack_id="U0JUSTIN01")
    c.puma = await make_user("Puma", "interview_manager", "U0PUMA0001")
    c.ana = await make_user("Ana", slack_id="U0ANA00001")
    c.pietro = await Caller(name="Pietro", nameKey="pietro").insert()
    return c


def test_constants():
    assert CHOOSE_REFUSED == "Only the owner or an interview manager can assign the caller."
    assert ROUND_GONE == "This round is gone."
    assert actions.HANDLERS["coord_choose_caller"] is actions.choose_caller
    assert actions.HANDLERS["coord_confirm"] is actions.confirm


# --- choose caller ----------------------------------------------------------------------------------------


async def test_owner_picks_a_caller_through_the_round_field(slack, spies, cast):
    iv = await make_interview(cast.justin)
    await actions.process_coordination_click(click("coord_choose_caller", iv, "U0JUSTIN01", str(cast.ana.id)))
    entry = await tip(iv)
    c = entry.caller
    assert (c.callerName, c.callerId, c.callerKind) == ("Ana", str(cast.ana.id), "user")
    assert c.method == "video" and c.methodValue == LINK  # the round's method and link survive
    assert entry.confirmed is False
    spies.update.assert_awaited_once()
    spies.assign.assert_not_awaited()
    assert texts(slack) == ["Caller set: Ana."]
    slack.sync.assert_awaited()


async def test_manager_picks_through_the_manager_endpoint(slack, spies, cast):
    iv = await make_interview(cast.justin)
    await actions.process_coordination_click(click("coord_choose_caller", iv, "U0PUMA0001", str(cast.pietro.id)))
    c = (await tip(iv)).caller
    assert (c.callerName, c.callerKind) == ("Pietro", "external")
    assert c.method == "video" and c.methodValue == LINK
    spies.assign.assert_awaited_once()
    spies.update.assert_not_awaited()
    assert texts(slack) == ["Caller set: Pietro."]
    slack.sync.assert_awaited()


@pytest.mark.parametrize("who", ["U0JUSTIN01", "U0PUMA0001"])
async def test_self_turns_the_caller_request_off(slack, cast, who):
    iv = await make_interview(cast.justin)
    await actions.process_coordination_click(click("coord_choose_caller", iv, who, "self"))
    assert (await tip(iv)).caller.enabled is False
    assert texts(slack) == ["No caller (Self) — the caller request is off."]
    slack.sync.assert_awaited()


@pytest.mark.parametrize("who", ["U0JUSTIN01", "U0PUMA0001"])
async def test_pick_on_a_self_round_turns_it_back_on(slack, cast, who):
    iv = await make_interview(cast.justin, enabled=False)
    await actions.process_coordination_click(click("coord_choose_caller", iv, who, str(cast.ana.id)))
    c = (await tip(iv)).caller
    assert c.enabled is True and c.callerName == "Ana"
    assert texts(slack) == ["Caller set: Ana."]


async def test_stranger_is_refused(slack, cast):
    iv = await make_interview(cast.justin)
    await actions.process_coordination_click(click("coord_choose_caller", iv, "U0ANA00001", str(cast.pietro.id)))
    assert texts(slack) == [CHOOSE_REFUSED]
    c = (await tip(iv)).caller
    assert c.callerName == "TBD" and c.callerId is None


async def test_owner_cannot_pick_themselves(slack, cast):
    iv = await make_interview(cast.justin)
    await actions.process_coordination_click(click("coord_choose_caller", iv, "U0JUSTIN01", str(cast.justin.id)))
    assert texts(slack) == [SELF_REFUSAL]
    assert (await tip(iv)).caller.callerName == "TBD"


@pytest.mark.parametrize("bad", ["", "nocolon", "a:b", "a:b:c:d"])
async def test_malformed_value_is_round_gone(slack, cast, bad):
    await actions.process_coordination_click(
        Click("coord_choose_caller", bad, "U0PUMA0001", RESPONSE_URL, "T.1", "C1", "1.2"),
    )
    assert texts(slack) == [ROUND_GONE]


async def test_missing_interview_and_round_are_round_gone(slack, cast):
    iv = await make_interview(cast.justin)
    ghost = Click(
        "coord_choose_caller", f"{PydanticObjectId()}:{iv.stageHistory[-1].id}:self", "U0PUMA0001",
        RESPONSE_URL, "T.1", "C1", "1.2",
    )
    await actions.process_coordination_click(ghost)
    no_round = Click("coord_choose_caller", f"{iv.id}:nope:self", "U0PUMA0001", RESPONSE_URL, "T.1", "C1", "1.2")
    await actions.process_coordination_click(no_round)
    assert texts(slack) == [ROUND_GONE, ROUND_GONE]
    assert (await tip(iv)).caller.enabled is True


async def test_http_exception_detail_reaches_the_clicker(slack, cast):
    iv = await make_interview(cast.justin)
    gone = PydanticObjectId()  # a caller who is on no list
    await actions.process_coordination_click(click("coord_choose_caller", iv, "U0PUMA0001", str(gone)))
    assert texts(slack) == ["That caller is no longer on the list."]


# --- confirm ----------------------------------------------------------------------------------------------


async def test_confirm_from_the_post(slack, cast):
    iv = await make_interview(cast.justin, caller_id=str(cast.ana.id), kind="user", name="Ana")
    await actions.process_coordination_click(click("coord_confirm", iv, "U0JUSTIN01"))  # owner may confirm too
    assert (await tip(iv)).confirmed is True
    assert slack == []  # confirm_round syncs the post; no extra reply
    slack.sync.assert_awaited()


async def test_confirm_on_an_already_confirmed_round_says_so(slack, cast):
    iv = await make_interview(cast.justin, caller_id=str(cast.ana.id), kind="user", name="Ana")
    await actions.process_coordination_click(click("coord_confirm", iv, "U0ANA00001"))
    assert (await tip(iv)).confirmed is True and texts(slack) == []
    slack.sync.reset_mock()
    await actions.process_coordination_click(click("coord_confirm", iv, "U0ANA00001"))  # an old click, same button
    assert texts(slack) == ["Already confirmed."]
    assert (await tip(iv)).confirmed is True
    slack.sync.assert_not_awaited()


async def test_assigned_caller_and_stranger_confirm(slack, cast):
    iv = await make_interview(cast.justin, caller_id=str(cast.ana.id), kind="user", name="Ana")
    stranger = await make_user("Bob", slack_id="U0BOB000001")
    assert stranger
    await actions.process_coordination_click(click("coord_confirm", iv, "U0BOB000001"))
    assert texts(slack) == [CONFIRM_REFUSAL] and (await tip(iv)).confirmed is False
    await actions.process_coordination_click(click("coord_confirm", iv, "U0ANA00001"))
    assert (await tip(iv)).confirmed is True
    assert texts(slack) == [CONFIRM_REFUSAL]


async def test_confirm_tbd_round(slack, cast):
    iv = await make_interview(cast.justin)
    await actions.process_coordination_click(click("coord_confirm", iv, "U0PUMA0001"))
    assert texts(slack) == ["Assign a caller first"]
    assert (await tip(iv)).confirmed is False


async def test_confirm_missing_round_replies_the_error(slack, cast):
    iv = await make_interview(cast.justin)
    bad = Click("coord_confirm", f"{iv.id}:nope", "U0PUMA0001", RESPONSE_URL, "T.1", "C1", "1.2")
    await actions.process_coordination_click(bad)
    assert texts(slack) == ["stage not found"]
    malformed = Click("coord_confirm", "nocolon", "U0PUMA0001", RESPONSE_URL, "T.1", "C1", "1.2")
    await actions.process_coordination_click(malformed)
    assert texts(slack) == ["stage not found", ROUND_GONE]
