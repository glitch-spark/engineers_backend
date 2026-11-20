"""After-call answers (Completed / Canceled / Rescheduled…) and results (Passed / Rejected) from Slack, and the three
form submissions. Each write calls `update_interview_stage`, the router function the app calls, so the post
re-renders through `sync_caller_thread` (spied). Slack is never reached: `slack_bot._slack_api` is a recorder and
`actions._post_ephemeral` is patched."""

import asyncio
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

import pytest  # noqa: E402
from beanie import PydanticObjectId, init_beanie  # noqa: E402
from mongomock_motor import AsyncMongoMockClient  # noqa: E402

from app import slack_bot  # noqa: E402
from app.coordination import actions, modals  # noqa: E402
from app.coordination.actions import (  # noqa: E402
    ALREADY,
    MOVED,
    NEEDS_COMPLETED,
    NOT_LINKED,
    ROUND_GONE,
    TRY_AGAIN,
    UPDATE_REFUSED,
    Click,
)
from app.coordination.modals import Origin  # noqa: E402
from app.models import Account, ResultRoundupPost, UserExtra  # noqa: E402
from app.models.caller import Caller  # noqa: E402
from app.models.interview import Interview, InterviewCaller, InterviewStageEvent, SlackThread  # noqa: E402
from app.models.interview_day_post import InterviewDayPost  # noqa: E402
from app.models.interview_question import InterviewQuestion  # noqa: E402
from app.models.round_slack_post import RoundSlackPost  # noqa: E402
from app.interview_day import home  # noqa: E402
from app.routers import interviews  # noqa: E402
from app.slack_bot import slack_time  # noqa: E402

pytestmark = [
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]

UTC = timezone.utc
START = datetime(2026, 10, 7, 15, 0, tzinfo=UTC)  # long past: the ask is due
NEW = datetime(2026, 11, 18, 15, 15, tzinfo=UTC)
RESPONSE_URL = "https://hooks.slack.com/actions/T1/1/abc"
POST_TS = "100.1"
ASK_TS = "200.2"


@pytest.fixture
async def db():
    database = AsyncMongoMockClient()["coord-answers-unit"]
    await init_beanie(
        database=database,
        document_models=[Interview, UserExtra, Account, InterviewQuestion, InterviewDayPost, Caller, ResultRoundupPost,
                         RoundSlackPost],
    )
    return database


class Slack:
    def __init__(self):
        self.calls: list[tuple[str, dict]] = []  # slack_bot._slack_api
        self.replies: list[tuple[str, dict]] = []  # actions._post_ephemeral (response_url)
        self.fail: dict[str, Exception] = {}

    def of(self, method):
        return [body for m, body in self.calls if m == method]


@pytest.fixture
def slack(db, monkeypatch):
    rec = Slack()
    rec.sync = AsyncMock(return_value=False)
    rec.refresh = AsyncMock(return_value=False)
    monkeypatch.setattr(interviews, "sync_caller_thread", rec.sync)
    monkeypatch.setattr(interviews, "refresh_today", rec.refresh)

    async def api(method, body):
        rec.calls.append((method, body))
        if method in rec.fail:
            raise rec.fail[method]
        return {"ok": True}

    async def respond(url, payload):
        rec.replies.append((url, payload))

    monkeypatch.setattr(slack_bot, "_slack_api", api)
    with patch.object(actions, "_post_ephemeral", respond):
        yield rec


async def make_user(name, role="staff", slack_id=None, tz=None) -> UserExtra:
    return await UserExtra(
        name=name, email=f"{name.lower()}@x.io", role=role, slackUserId=slack_id, slackTimezone=tz,
    ).insert()


@pytest.fixture
async def cast(db):
    class Cast:
        pass

    c = Cast()
    c.justin = await make_user("Justin", slack_id="U0JUSTIN01", tz="America/New_York")
    c.puma = await make_user("Puma", "interview_manager", "U0PUMA0001")
    c.ana = await make_user("Ana", slack_id="U0ANA00001")
    c.bob = await make_user("Bob", slack_id="U0BOB00001")
    return c


async def make_interview(cast, *, status="scheduled", start=START, confirmed=True) -> Interview:
    caller = InterviewCaller(
        enabled=True, callerId=str(cast.ana.id), callerName="Ana", callerKind="user", startsAt=start,
        method="video", methodValue="https://meet.x/abc",
    )
    entry = InterviewStageEvent(
        stage="intro", at=start, scheduledAt=start, endsAt=start + timedelta(hours=1), status=status, caller=caller,
        confirmed=confirmed,
    )
    return await Interview(
        accountId=PydanticObjectId(), createdBy=cast.justin.id, scheduledAt=start, companyName="Acme",
        stageHistory=[entry.model_dump()], stage="intro", status=status,
        slackThread=SlackThread(channel="C1", ts=POST_TS),
    ).insert()


def ref(iv) -> str:
    return f"{iv.id}:{iv.stageHistory[-1].id}"


def click(action_id, iv, who, *, ts=ASK_TS) -> Click:
    return Click(action_id, ref(iv), who, RESPONSE_URL, "T.1", "C1", ts)


async def tip(iv) -> InterviewStageEvent:
    return (await Interview.get(iv.id)).stageHistory[-1]


def status_of(entry) -> str:
    return getattr(entry.status, "value", entry.status)


def ephemeral_texts(rec) -> list[str]:
    return [p["text"] for _, p in rec.replies if p.get("replace_original") is False]


def replaced(rec) -> list[dict]:
    return [p for _, p in rec.replies if p.get("replace_original") is True]


def submission(view: dict, *, note=None, date=None, time=None, reason=None) -> dict:
    values = {"note": {"note_input": {"type": "plain_text_input", "value": note}}}
    if reason is not None:
        values["reason"] = {"reason_input": {"type": "static_select", "selected_option": {"value": reason}}}
    if view["callback_id"] == "coord_reschedule":
        values["date"] = {"date_input": {"type": "datepicker", "selected_date": date}}
        values["time"] = {"time_input": {"type": "timepicker", "selected_time": time}}
    return {**view, "state": {"values": values}}


def origin(iv, *, ts=ASK_TS, tz="America/New_York") -> Origin:
    return Origin(ref(iv), "C1", ts, tz)


def test_constants():
    assert UPDATE_REFUSED == "Only the owner or an interview manager can update this round."
    assert NEEDS_COMPLETED == "Mark the round completed first."
    assert ALREADY == "Already updated: {Label}."
    assert MOVED == "This round was moved — answer the new ask after it ends."
    for action_id, name in [
        ("coord_after_completed", "after_completed"), ("coord_after_canceled", "after_canceled"),
        ("coord_after_rescheduled", "after_rescheduled"), ("coord_result_passed", "result_passed"),
        ("coord_after_passed", "after_passed"), ("coord_after_rejected", "after_rejected"),
        ("coord_result_rejected", "result_rejected"), ("coord_followed_up", "followed_up"),
        ("coord_result_no_answer", "result_no_answer"),
    ]:
        assert actions.HANDLERS[action_id] is getattr(actions, name)


def test_answer_lines(cast):
    j = cast.justin
    assert actions.answer_line("completed", j) == "✅ Completed — by <@U0JUSTIN01>"
    assert actions.answer_line("canceled", j) == "❌ Canceled — by <@U0JUSTIN01>"
    assert actions.answer_line("canceled", j, note="a & b") == "❌ Canceled — by <@U0JUSTIN01> · a &amp; b"
    assert actions.answer_line("rescheduled", j, new_start=NEW) == f"🔁 Rescheduled to {slack_time(NEW)} — by <@U0JUSTIN01>"
    assert actions.answer_line("passed", j) == "🎉 Passed — by <@U0JUSTIN01>"
    assert actions.answer_line("rejected", j, note="ignored") == "❌ Rejected — by <@U0JUSTIN01>"


# --- after-call ask: Completed ----------------------------------------------------------------------------


async def test_owner_marks_completed_from_the_ask(slack, cast):
    iv = await make_interview(cast)
    await actions.process_coordination_click(click("coord_after_completed", iv, "U0JUSTIN01"))
    assert status_of(await tip(iv)) == "completed"
    out = replaced(slack)
    assert len(out) == 1
    assert out[0]["text"] == "✅ Completed — by <@U0JUSTIN01>"
    assert [url for url, p in slack.replies if p.get("replace_original")] == [RESPONSE_URL]
    assert ephemeral_texts(slack) == []
    slack.sync.assert_awaited()


async def test_manager_marks_completed_on_someone_elses_round(slack, cast):
    iv = await make_interview(cast)
    await actions.process_coordination_click(click("coord_after_completed", iv, "U0PUMA0001"))
    assert status_of(await tip(iv)) == "completed"
    assert [p["text"] for p in replaced(slack)] == ["✅ Completed — by <@U0PUMA0001>"]
    slack.sync.assert_awaited()


@pytest.mark.parametrize("who", ["U0ANA00001", "U0BOB00001"])
async def test_caller_and_stranger_are_refused(slack, cast, who):
    iv = await make_interview(cast)
    await actions.process_coordination_click(click("coord_after_completed", iv, who))
    assert ephemeral_texts(slack) == [UPDATE_REFUSED]
    assert replaced(slack) == []
    assert status_of(await tip(iv)) == "scheduled"


async def test_stale_ask_says_already_updated(slack, cast, monkeypatch):
    iv = await make_interview(cast, status="canceled")
    spy = AsyncMock(wraps=interviews.update_interview_stage)
    monkeypatch.setattr(interviews, "update_interview_stage", spy)
    await actions.process_coordination_click(click("coord_after_completed", iv, "U0JUSTIN01"))
    assert ephemeral_texts(slack) == ["Already updated: Canceled."]
    assert [p["text"] for p in replaced(slack)] == ["Already updated: Canceled"]
    spy.assert_not_awaited()


@pytest.mark.parametrize("action_id", ["coord_after_completed", "coord_after_canceled", "coord_after_rescheduled"])
async def test_old_ask_after_a_move_writes_nothing(slack, cast, action_id):
    """F21: the round was moved to a later time, so the old ask must not mark it (or open a form)."""
    later = datetime.now(UTC).replace(microsecond=0) + timedelta(days=2)
    iv = await make_interview(cast, start=later)
    await actions.process_coordination_click(click(action_id, iv, "U0JUSTIN01"))
    assert ephemeral_texts(slack) == [MOVED]
    assert [p["text"] for p in replaced(slack)] == [MOVED]
    assert slack.of("views.open") == []
    assert status_of(await tip(iv)) == "scheduled"
    slack.sync.assert_not_awaited()


async def test_round_gone(slack, cast):
    iv = await make_interview(cast)
    bad = Click("coord_after_completed", f"{iv.id}:{'f' * 32}", "U0JUSTIN01", RESPONSE_URL, "T.1", "C1", ASK_TS)
    await actions.process_coordination_click(bad)
    assert ephemeral_texts(slack) == [ROUND_GONE]


async def test_answering_a_home_message_keeps_which_round_it_was(slack, cast):
    """An ask that is the round's home is top-level in the channel: its answer still names the round."""
    iv = await make_interview(cast)
    await home.record_home(iv.stageHistory[-1].id, str(iv.id), START, "C1", ASK_TS)
    await actions.process_coordination_click(click("coord_after_completed", iv, "U0JUSTIN01"))
    assert [p["text"] for p in replaced(slack)] == [f"Acme · Intro · {slack_time(START)} — ✅ Completed — by <@U0JUSTIN01>"]


async def test_answering_a_home_from_a_form_keeps_which_round_it_was(slack, cast):
    iv = await make_interview(cast, status="completed")
    await home.record_home(iv.stageHistory[-1].id, str(iv.id), START, "C1", ASK_TS)
    view = submission(modals.reject_view(origin(iv), "Acme · Intro"), note="No fit")
    await modals.process_coordination_submission(view, "U0JUSTIN01")
    assert [b["text"] for b in slack.of("chat.update")] == [
        f"Acme · Intro · {slack_time(START)} — ❌ Rejected — by <@U0JUSTIN01>"]


# --- after-call ask: Passed / Rejected straight from a scheduled round -------------------------------------


async def test_after_passed_writes_passed_on_an_ended_scheduled_round(slack, cast):
    iv = await make_interview(cast)
    await actions.process_coordination_click(click("coord_after_passed", iv, "U0JUSTIN01"))
    assert status_of(await tip(iv)) == "passed"
    assert [p["text"] for p in replaced(slack)] == ["🎉 Passed — by <@U0JUSTIN01>"]


async def test_after_passed_refuses_a_round_not_ended_yet(slack, cast):
    later = datetime.now(UTC).replace(microsecond=0) + timedelta(days=2)
    iv = await make_interview(cast, start=later)
    await actions.process_coordination_click(click("coord_after_passed", iv, "U0JUSTIN01"))
    assert ephemeral_texts(slack) == [MOVED]
    assert status_of(await tip(iv)) == "scheduled"


async def test_after_passed_refuses_strangers(slack, cast):
    iv = await make_interview(cast)
    await actions.process_coordination_click(click("coord_after_passed", iv, "U0BOB00001"))
    assert ephemeral_texts(slack) == [UPDATE_REFUSED]
    assert status_of(await tip(iv)) == "scheduled"


async def test_after_rejected_opens_the_reject_form(slack, cast):
    iv = await make_interview(cast)
    await actions.process_coordination_click(click("coord_after_rejected", iv, "U0JUSTIN01"))
    view = slack.of("views.open")[0]["view"]
    assert view["callback_id"] == "coord_reject"
    assert Origin.from_metadata(view["private_metadata"]).ts == ASK_TS


async def test_reject_form_accepts_an_ended_scheduled_round(slack, cast):
    iv = await make_interview(cast)
    view = submission(modals.reject_view(origin(iv), "Acme · Intro"), note="No fit")
    await modals.process_coordination_submission(view, "U0JUSTIN01")
    entry = await tip(iv)
    assert status_of(entry) == "rejected" and entry.statusNote == "No fit"


async def test_reject_form_refuses_a_scheduled_round_not_ended(slack, cast):
    later = datetime.now(UTC).replace(microsecond=0) + timedelta(days=2)
    iv = await make_interview(cast, start=later)
    view = submission(modals.reject_view(origin(iv), "Acme · Intro"))
    await modals.process_coordination_submission(view, "U0JUSTIN01")
    assert slack.of("chat.postEphemeral") == [{"channel": "C1", "user": "U0JUSTIN01", "text": MOVED}]
    assert status_of(await tip(iv)) == "scheduled"


# --- after-call ask: the two forms ------------------------------------------------------------------------


async def test_canceled_opens_the_form(slack, cast):
    iv = await make_interview(cast)
    await actions.process_coordination_click(click("coord_after_canceled", iv, "U0JUSTIN01"))
    opened = slack.of("views.open")
    assert len(opened) == 1
    assert opened[0]["trigger_id"] == "T.1"
    view = opened[0]["view"]
    assert view["callback_id"] == "coord_cancel"
    meta = Origin.from_metadata(view["private_metadata"])
    assert (meta.ref, meta.channel, meta.ts) == (ref(iv), "C1", ASK_TS)
    assert "Acme · Intro" in view["blocks"][0]["elements"][0]["text"]
    assert slack.replies == []
    assert status_of(await tip(iv)) == "scheduled"


async def test_rescheduled_opens_the_form_in_the_owners_zone(slack, cast):
    iv = await make_interview(cast)
    await actions.process_coordination_click(click("coord_after_rescheduled", iv, "U0PUMA0001"))
    view = slack.of("views.open")[0]["view"]
    assert view["callback_id"] == "coord_reschedule"
    assert Origin.from_metadata(view["private_metadata"]).tz == "America/New_York"
    time_block = next(b for b in view["blocks"] if b.get("block_id") == "time")
    assert time_block["label"]["text"] == "Time (America/New_York)"
    assert time_block["element"]["initial_time"] == "11:00"  # 15:00Z in New York (EDT)
    date_block = next(b for b in view["blocks"] if b.get("block_id") == "date")
    assert date_block["element"]["initial_date"] == "2026-10-07"


@pytest.mark.parametrize("action_id", ["coord_after_canceled", "coord_after_rescheduled", "coord_result_rejected"])
async def test_form_does_not_open_for_strangers(slack, cast, action_id):
    iv = await make_interview(cast, status="completed" if action_id == "coord_result_rejected" else "scheduled")
    await actions.process_coordination_click(click(action_id, iv, "U0BOB00001"))
    assert slack.of("views.open") == []
    assert ephemeral_texts(slack) == [UPDATE_REFUSED]


async def test_expired_trigger(slack, cast):
    slack.fail["views.open"] = RuntimeError("expired_trigger_id")
    iv = await make_interview(cast)
    await actions.process_coordination_click(click("coord_after_canceled", iv, "U0JUSTIN01"))
    assert ephemeral_texts(slack) == ["Couldn't open the form — click again."]


# --- results ----------------------------------------------------------------------------------------------


async def test_passed_from_the_post_relies_on_the_rerender(slack, cast):
    iv = await make_interview(cast, status="completed")
    await actions.process_coordination_click(click("coord_result_passed", iv, "U0JUSTIN01", ts=POST_TS))
    assert status_of(await tip(iv)) == "passed"
    assert replaced(slack) == []
    assert [b for b in slack.of("chat.update") if b.get("ts") == POST_TS] == []
    slack.sync.assert_awaited()


async def test_passed_from_the_reminder_replaces_it(slack, cast):
    iv = await make_interview(cast, status="completed")
    await actions.process_coordination_click(click("coord_result_passed", iv, "U0PUMA0001"))
    assert status_of(await tip(iv)) == "passed"
    assert [p["text"] for p in replaced(slack)] == ["🎉 Passed — by <@U0PUMA0001>"]


async def test_passed_before_completed(slack, cast):
    iv = await make_interview(cast)
    await actions.process_coordination_click(click("coord_result_passed", iv, "U0JUSTIN01"))
    assert ephemeral_texts(slack) == ["Mark the round completed first."]
    assert status_of(await tip(iv)) == "scheduled"


@pytest.mark.parametrize("action_id", ["coord_result_passed", "coord_result_rejected"])
async def test_result_already_set(slack, cast, action_id):
    iv = await make_interview(cast, status="rejected")
    await actions.process_coordination_click(click(action_id, iv, "U0JUSTIN01"))
    assert ephemeral_texts(slack) == ["Result already set: Rejected."]
    assert slack.of("views.open") == []


@pytest.mark.parametrize("action_id", ["coord_result_passed", "coord_result_rejected"])
async def test_result_already_set_on_an_old_roundup_drops_the_row(slack, cast, action_id):
    """The result was set in the app after the roundup was posted: the click is answered, and the roundup is
    refreshed so the row goes."""
    iv = await make_interview(cast, status="passed")
    roundup = await ResultRoundupPost(friday="2026-11-13", channel="C1", ts="300.3", refs=[ref(iv)], rendered="x").insert()
    refresh = AsyncMock()
    with patch("app.coordination.chase.refresh_roundup", refresh):
        await actions.process_coordination_click(click(action_id, iv, "U0JUSTIN01", ts="300.3"))
    assert ephemeral_texts(slack) == ["Result already set: Passed."]
    refresh.assert_awaited_once()
    assert refresh.await_args.args[0].id == roundup.id
    assert slack.of("views.open") == []


async def test_rejected_opens_the_form(slack, cast):
    iv = await make_interview(cast, status="completed")
    await actions.process_coordination_click(click("coord_result_rejected", iv, "U0JUSTIN01", ts=POST_TS))
    view = slack.of("views.open")[0]["view"]
    assert view["callback_id"] == "coord_reject"
    assert Origin.from_metadata(view["private_metadata"]).ts == POST_TS


# --- the follow-up step -----------------------------------------------------------------------------------


@pytest.mark.parametrize("who", ["U0JUSTIN01", "U0PUMA0001"])
async def test_followed_up_records_it_and_rewrites_the_message(slack, cast, who):
    iv = await make_interview(cast, status="completed")
    await actions.process_coordination_click(click("coord_followed_up", iv, who))
    saved = await tip(iv)
    [entry] = saved.followUps
    assert entry.by == (cast.justin.id if who == "U0JUSTIN01" else cast.puma.id)
    assert status_of(saved) == "completed"
    [shown] = replaced(slack)
    assert shown["text"] == f"📨 <@{who}> followed up on {slack_time(entry.at)}."
    assert [b["type"] for b in shown["blocks"]] == ["section"]  # no buttons left
    assert ephemeral_texts(slack) == []


async def test_followed_up_is_refused_for_strangers(slack, cast):
    iv = await make_interview(cast, status="completed")
    await actions.process_coordination_click(click("coord_followed_up", iv, "U0BOB00001"))
    assert ephemeral_texts(slack) == [UPDATE_REFUSED]
    assert (await tip(iv)).followUps == [] and replaced(slack) == []


async def test_followed_up_twice_keeps_one_entry_and_settles_the_message(slack, cast):
    """Followed up in the app (or by an earlier click): the stale button records nothing more."""
    iv = await make_interview(cast, status="completed")
    await actions.process_coordination_click(click("coord_followed_up", iv, "U0JUSTIN01"))
    [first] = (await tip(iv)).followUps
    await actions.process_coordination_click(click("coord_followed_up", iv, "U0PUMA0001"))
    assert (await tip(iv)).followUps == [first]
    assert ephemeral_texts(slack) == ["Already followed up."]
    assert [p["text"] for p in replaced(slack)][-1] == f"📨 <@U0JUSTIN01> followed up on {slack_time(first.at)}."


async def test_followed_up_on_a_round_with_a_result_settles_the_message(slack, cast):
    iv = await make_interview(cast, status="passed")
    await actions.process_coordination_click(click("coord_followed_up", iv, "U0JUSTIN01"))
    assert ephemeral_texts(slack) == ["Result already set: Passed."]
    assert [p["text"] for p in replaced(slack)] == ["Result already set: Passed"]
    assert (await tip(iv)).followUps == []


async def test_followed_up_before_completed(slack, cast):
    iv = await make_interview(cast)
    await actions.process_coordination_click(click("coord_followed_up", iv, "U0JUSTIN01"))
    assert ephemeral_texts(slack) == [NEEDS_COMPLETED]
    assert (await tip(iv)).followUps == []


async def test_no_answer_closes_the_round_as_rejected_in_one_click(slack, cast):
    iv = await make_interview(cast, status="completed")
    await actions.process_coordination_click(click("coord_result_no_answer", iv, "U0JUSTIN01"))
    saved = await tip(iv)
    assert (status_of(saved), saved.statusReason) == ("rejected", "no_answer")
    assert saved.statusAt is not None
    assert slack.of("views.open") == []  # no form
    assert [p["text"] for p in replaced(slack)] == ["❌ Rejected — by <@U0JUSTIN01> · No answer from the company"]


async def test_no_answer_is_refused_for_strangers_and_on_a_settled_round(slack, cast):
    iv = await make_interview(cast, status="completed")
    await actions.process_coordination_click(click("coord_result_no_answer", iv, "U0BOB00001"))
    assert status_of(await tip(iv)) == "completed"
    done = await make_interview(cast, status="passed")
    await actions.process_coordination_click(click("coord_result_no_answer", done, "U0JUSTIN01"))
    assert status_of(await tip(done)) == "passed"
    assert ephemeral_texts(slack) == [UPDATE_REFUSED, "Result already set: Passed."]


# --- submissions ------------------------------------------------------------------------------------------


async def test_cancel_submission(slack, cast):
    iv = await make_interview(cast)
    view = submission(modals.cancel_view(origin(iv), "Acme · Intro"), note="  Recruiter canceled ")
    await modals.process_coordination_submission(view, "U0JUSTIN01")
    entry = await tip(iv)
    assert status_of(entry) == "canceled" and entry.statusNote == "Recruiter canceled"
    updates = slack.of("chat.update")
    assert len(updates) == 1
    assert (updates[0]["channel"], updates[0]["ts"]) == ("C1", ASK_TS)
    assert updates[0]["text"] == "❌ Canceled — by <@U0JUSTIN01> · Recruiter canceled"
    assert updates[0]["blocks"][0]["text"]["text"] == updates[0]["text"]
    assert slack.of("chat.postEphemeral") == []
    slack.sync.assert_awaited()


async def test_reschedule_submission_moves_the_round(slack, cast):
    iv = await make_interview(cast)
    assert (await tip(iv)).confirmed is True
    view = submission(
        modals.reschedule_view(origin(iv), "Acme · Intro", start=START),
        date="2026-11-18", time="10:15", note="Moved by recruiter",
    )
    await modals.process_coordination_submission(view, "U0JUSTIN01")
    await asyncio.gather(*list(interviews._day_post_tasks))
    entry = await tip(iv)
    assert slack_bot._aware_utc(entry.scheduledAt) == NEW
    assert slack_bot._aware_utc(entry.endsAt) == NEW + timedelta(hours=1)  # the 1 h duration is kept
    assert status_of(entry) == "scheduled"
    assert entry.confirmed is True  # a reschedule keeps the assigned caller confirmed
    assert entry.statusNote == "Moved by recruiter"
    assert [b["text"] for b in slack.of("chat.update")] == [f"🔁 Rescheduled to {slack_time(NEW)} — by <@U0JUSTIN01>"]
    # The submitter sees the time actually saved, in the owner's zone.
    assert [(b["user"], b["text"]) for b in slack.of("chat.postEphemeral")] == [
        ("U0JUSTIN01", "Rescheduled to Wed 11/18 10:15 AM EST (GMT−5:00)."),
    ]
    slack.refresh.assert_awaited()
    slack.sync.assert_awaited()


async def test_reschedule_into_a_dst_gap_shows_the_saved_time(slack, cast):
    iv = await make_interview(cast)
    view = submission(modals.reschedule_view(origin(iv), "Acme · Intro", start=START), date="2027-03-14", time="02:30")
    await modals.process_coordination_submission(view, "U0JUSTIN01")
    saved = slack_bot._aware_utc((await tip(iv)).scheduledAt)
    assert saved == datetime(2027, 3, 14, 7, 30, tzinfo=UTC)  # 02:30 does not exist in New York that night
    assert [b["text"] for b in slack.of("chat.postEphemeral")] == ["Rescheduled to Sun 3/14 3:30 AM EDT (GMT−4:00)."]
    assert [b["text"] for b in slack.of("chat.update")] == [f"🔁 Rescheduled to {slack_time(saved)} — by <@U0JUSTIN01>"]


async def test_reschedule_without_a_note_keeps_the_note_unsent(slack, cast, monkeypatch):
    iv = await make_interview(cast)
    real = interviews.update_interview_stage
    sent: list[set] = []

    async def spy(iv_id, round_id, body, user):
        sent.append(set(body.model_fields_set))  # before the router fills in stage / status
        return await real(iv_id, round_id, body, user)

    monkeypatch.setattr(interviews, "update_interview_stage", spy)
    view = submission(modals.reschedule_view(origin(iv), "Acme · Intro", start=START), date="2026-11-18", time="10:15")
    await modals.process_coordination_submission(view, "U0PUMA0001")
    assert sent == [{"scheduledAt"}]
    assert slack_bot._aware_utc((await tip(iv)).scheduledAt) == NEW


async def test_manager_reschedule_uses_the_manager_path(slack, cast):
    iv = await make_interview(cast)
    view = submission(
        modals.reschedule_view(origin(iv), "Acme · Intro", start=START),
        date="2026-11-18", time="10:15", note="Moved by recruiter",
    )
    await modals.process_coordination_submission(view, "U0PUMA0001")
    entry = await tip(iv)
    assert slack_bot._aware_utc(entry.scheduledAt) == NEW
    assert [b["text"] for b in slack.of("chat.update")] == [f"🔁 Rescheduled to {slack_time(NEW)} — by <@U0PUMA0001>"]


async def test_rejected_submission_sets_feedback(slack, cast):
    iv = await make_interview(cast, status="completed")
    view = submission(modals.reject_view(origin(iv), "Acme · Intro"), note="Went with another candidate")
    await modals.process_coordination_submission(view, "U0PUMA0001")
    entry = await tip(iv)
    assert status_of(entry) == "rejected" and entry.statusNote == "Went with another candidate"
    updates = slack.of("chat.update")
    assert [(b["channel"], b["ts"], b["text"]) for b in updates] == [("C1", ASK_TS, "❌ Rejected — by <@U0PUMA0001>")]


async def test_rejected_submission_from_the_post_relies_on_the_rerender(slack, cast):
    iv = await make_interview(cast, status="completed")
    view = submission(modals.reject_view(origin(iv, ts=POST_TS), "Acme · Intro"))
    await modals.process_coordination_submission(view, "U0JUSTIN01")
    assert status_of(await tip(iv)) == "rejected"
    assert slack.of("chat.update") == []
    slack.sync.assert_awaited()


async def test_cancel_submission_saves_the_picked_reason(slack, cast):
    iv = await make_interview(cast)
    view = submission(modals.cancel_view(origin(iv), "Acme · Intro"), note="Role filled", reason="company")
    await modals.process_coordination_submission(view, "U0PUMA0001")  # a manager, on someone else's round
    entry = await tip(iv)
    assert (status_of(entry), entry.statusReason, entry.statusNote) == ("canceled", "company", "Role filled")
    assert slack.of("chat.postEphemeral") == []


async def test_reject_submission_saves_the_picked_reason(slack, cast):
    iv = await make_interview(cast, status="completed")
    view = submission(modals.reject_view(origin(iv), "Acme · Intro"), reason="other_candidate")
    await modals.process_coordination_submission(view, "U0JUSTIN01")
    entry = await tip(iv)
    assert (status_of(entry), entry.statusReason) == ("rejected", "other_candidate")


async def test_reschedule_submission_logs_the_picked_reason(slack, cast):
    iv = await make_interview(cast)
    view = submission(
        modals.reschedule_view(origin(iv), "Acme · Intro", start=START),
        date="2026-11-18", time="10:15", note="Moved by recruiter", reason="caller",
    )
    await modals.process_coordination_submission(view, "U0PUMA0001")
    entry = await tip(iv)
    assert slack_bot._aware_utc(entry.scheduledAt) == NEW and entry.statusReason is None
    [log] = entry.reschedules
    assert (slack_bot._aware_utc(log.fromAt), slack_bot._aware_utc(log.toAt)) == (START, NEW)
    assert (log.reason, log.note, log.by) == ("caller", "Moved by recruiter", cast.puma.id)
    assert [b["text"] for b in slack.of("chat.postEphemeral")] == ["Rescheduled to Wed 11/18 10:15 AM EST (GMT−5:00)."]


async def test_wrong_reason_in_a_submission_is_refused(slack, cast):
    iv = await make_interview(cast)
    view = submission(modals.cancel_view(origin(iv), "Acme · Intro"), reason="no_answer")  # a rejected key
    await modals.process_coordination_submission(view, "U0JUSTIN01")
    assert [b["text"] for b in slack.of("chat.postEphemeral")] == ["invalid statusReason"]
    assert status_of(await tip(iv)) == "scheduled"


async def test_submission_refused_tells_the_user(slack, cast):
    iv = await make_interview(cast)
    view = submission(modals.cancel_view(origin(iv), "Acme · Intro"), note="forged")
    await modals.process_coordination_submission(view, "U0BOB00001")
    told = slack.of("chat.postEphemeral")
    assert told == [{"channel": "C1", "user": "U0BOB00001", "text": UPDATE_REFUSED}]
    entry = await tip(iv)
    assert status_of(entry) == "scheduled" and not entry.statusNote
    assert slack.of("chat.update") == []


async def test_stale_submission_says_already_updated(slack, cast):
    iv = await make_interview(cast, status="canceled")
    view = submission(modals.reject_view(origin(iv), "Acme · Intro"))
    await modals.process_coordination_submission(view, "U0JUSTIN01")
    assert [b["text"] for b in slack.of("chat.postEphemeral")] == ["Already updated: Canceled."]
    assert status_of(await tip(iv)) == "canceled"


@pytest.mark.parametrize("kind", ["cancel", "reschedule"])
async def test_form_from_an_old_ask_cannot_apply_after_a_move(slack, cast, monkeypatch, kind):
    """The form was opened from the old ask; the round was then moved later. The submission writes nothing."""
    iv = await make_interview(cast)
    if kind == "cancel":
        view = submission(modals.cancel_view(origin(iv), "Acme · Intro"), note="Recruiter canceled")
    else:
        view = submission(modals.reschedule_view(origin(iv), "Acme · Intro", start=START),
                          date="2026-11-18", time="10:15")
    later = datetime.now(UTC).replace(microsecond=0) + timedelta(days=2)
    moved = await Interview.get(iv.id)
    moved.stageHistory[-1].scheduledAt = later
    moved.stageHistory[-1].endsAt = later + timedelta(hours=1)
    await moved.save()
    spy = AsyncMock(wraps=interviews.update_interview_stage)
    monkeypatch.setattr(interviews, "update_interview_stage", spy)
    await modals.process_coordination_submission(view, "U0JUSTIN01")
    spy.assert_not_awaited()
    assert slack.of("chat.postEphemeral") == [{"channel": "C1", "user": "U0JUSTIN01", "text": MOVED}]
    assert slack.of("chat.update") == []
    entry = await tip(iv)
    assert status_of(entry) == "scheduled" and slack_bot._aware_utc(entry.scheduledAt) == later


async def test_malformed_submission_tries_again(slack, cast):
    iv = await make_interview(cast)
    view = submission(modals.reschedule_view(origin(iv), "Acme · Intro", start=START), date="nope", time="10:15")
    await modals.process_coordination_submission(view, "U0JUSTIN01")
    assert slack.of("chat.postEphemeral") == [{"channel": "C1", "user": "U0JUSTIN01", "text": TRY_AGAIN}]
    assert slack_bot._aware_utc((await tip(iv)).scheduledAt) == START


async def test_submission_for_a_gone_round(slack, cast):
    iv = await make_interview(cast)
    gone = Origin(f"{iv.id}:{'f' * 32}", "C1", ASK_TS, "America/New_York")
    await modals.process_coordination_submission(submission(modals.cancel_view(gone, "x")), "U0JUSTIN01")
    assert [b["text"] for b in slack.of("chat.postEphemeral")] == [ROUND_GONE]


async def test_submission_from_an_unlinked_user(slack, cast):
    iv = await make_interview(cast)
    await modals.process_coordination_submission(submission(modals.cancel_view(origin(iv), "x")), "U0NOBODY01")
    assert [b["text"] for b in slack.of("chat.postEphemeral")] == [NOT_LINKED]
    assert status_of(await tip(iv)) == "scheduled"


async def test_router_refusal_reaches_the_submitter(slack, cast):
    iv = await make_interview(cast)
    view = submission(modals.cancel_view(origin(iv), "x"), note="x" * 3000)  # past the router's note limit
    await modals.process_coordination_submission(view, "U0JUSTIN01")
    told = [b["text"] for b in slack.of("chat.postEphemeral")]
    assert len(told) == 1 and "at most" in told[0]
    assert status_of(await tip(iv)) == "scheduled"
