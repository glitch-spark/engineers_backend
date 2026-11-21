"""The Slack standup form ("Write my update"): the view built from the day's draft, reading and checking a submission,
and the click / submission handlers saving through plan_save (mongomock-motor; Slack faked)."""
import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

import json
from datetime import date, datetime

import pytest
from beanie import init_beanie
from mongomock_motor import AsyncMongoMockClient

from app import plan_bidder, slack_bot
from app.coordination import actions
from app.models import Account, UserExtra
from app.models.bid import Bid
from app.models.bidder import Bidder
from app.models.daily_plan import DailyPlan
from app.models.interview import Interview
from app.models.interview_question import InterviewQuestion
from app.models.weekly_plan import WeeklyPlan
from app.routers import daily_plans
from app.routers.daily_plans import put_day
from app.routers.weekly_plans import put_week
from app.standup.modal import (
    STANDUP_VIEW, _day, build_view, process_standup_click, process_standup_submission, read_submission, submission_errors,
)

pytestmark = [
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]

WEEK = "2026-11-09"
TUE, SAT = date(2026, 11, 10), date(2026, 11, 14)
SLACK_ID = "U0ANA0001"
RESPONSE_URL = "https://hooks.slack.com/actions/T1/1/abc"


@pytest.fixture
async def db(monkeypatch):
    await init_beanie(database=AsyncMongoMockClient()["standup-modal-unit"], document_models=[
        UserExtra, Account, Bidder, Bid, DailyPlan, WeeklyPlan, Interview, InterviewQuestion])
    monkeypatch.setattr(daily_plans, "_today", lambda: date(2026, 11, 14))
    monkeypatch.setattr(plan_bidder, "RELEASE_DAY", date(2026, 12, 1))
    calls = []

    async def fake_sync(plan, owner, done, rounds):
        calls.append(plan.slackTs)
        if plan.slackTs is None:
            plan.slackChannel, plan.slackTs = "C1", "ts1"
            await plan.save()

    monkeypatch.setattr(daily_plans, "_sync_slack", fake_sync)
    return calls


class FakeSlack:
    def __init__(self):
        self.calls, self.replies, self.told = [], [], []

    async def api(self, method, body):
        self.calls.append((method, body))
        return {"ok": True, "view": {"id": "V1"}}

    @property
    def modals(self):
        """The forms shown: a loading view opens at once (the trigger id lasts 3 s), then the form replaces it."""
        return [(body["view_id"], body["view"]) for method, body in self.calls if method == "views.update"]

    async def reply(self, url, text):
        self.replies.append(text)

    async def tell(self, channel, user, text):
        self.told.append(text)


@pytest.fixture
def slack(monkeypatch):
    fake = FakeSlack()
    monkeypatch.setattr(slack_bot, "_slack_api", fake.api)
    monkeypatch.setattr(actions, "reply", fake.reply)
    monkeypatch.setattr(actions, "tell", fake.tell)
    return fake


@pytest.fixture
async def me(db):
    return await UserExtra(name="Ana", email="ana@x.io", slackUserId=SLACK_ID).insert()


async def week(me, routines=None, goals=None):
    out = await put_week(WEEK, me, {
        "goal": {"bidsSelf": 100, "bidsBidder": 50},
        "routines": routines or [{"text": "Bidder review"}, {"text": "Citizen contact", "kind": "count", "target": 50}],
        "weeklyGoals": goals or [{"text": "Email outreach", "kind": "count", "target": 250}],
        "recapNotes": "",
    })
    return [r["id"] for r in out["routines"]], [g["id"] for g in out["weeklyGoals"]]


def by_id(view):
    return {b["block_id"]: b for b in view["blocks"] if "block_id" in b}


def state(view, **typed):
    """The submitted state of `view`: each input's initial value, overridden by `typed` (block_id -> value; a bool
    ticks or unticks a checkbox)."""
    values = {}
    for b in view["blocks"]:
        if b["type"] != "input":
            continue
        el, bid = b["element"], b["block_id"]
        if el["type"] == "checkboxes":
            ticked = typed.get(bid, bool(el.get("initial_options")))
            values[bid] = {el["action_id"]: {"type": "checkboxes", "selected_options": el["options"] if ticked else []}}
        else:
            value = typed.get(bid, el.get("initial_value"))
            values[bid] = {el["action_id"]: {"type": el["type"], "value": value}}
    return {**view, "state": {"values": values}}


async def stored(me, day):
    return await DailyPlan.find_one({"userId": me.id, "date": datetime(day.year, day.month, day.day)})


async def test_view_prefilled_from_draft(me):
    (review, contact), (goal,) = await week(me)
    us = await Account(name="US Main", createdBy=me.id, region="US").insert()
    view = await build_view(me, TUE)
    assert view["callback_id"] == STANDUP_VIEW
    assert view["title"]["text"] == "Daily standup" and view["submit"]["text"] == "Post update"
    assert json.loads(view["private_metadata"]) == {"day": "2026-11-10"}
    blocks = by_id(view)
    assert blocks[f"item:{review}"]["element"]["type"] == "checkboxes"
    assert "initial_options" not in blocks[f"item:{review}"]["element"]
    assert blocks[f"item:{contact}"]["element"]["type"] == "number_input"
    assert "/ 50" in blocks[f"item:{contact}"]["hint"]["text"]
    assert "/ 250" in blocks[f"item:{goal}"]["hint"]["text"]
    assert blocks[f"self:{us.id}"]["element"]["type"] == "number_input"
    assert blocks[f"item:{contact}"]["element"]["min_value"] == "0"
    assert blocks[f"item:{contact}"]["element"]["max_value"] == "100000"
    assert blocks["notes"]["element"]["multiline"] is True
    context = " ".join(e["text"] for b in view["blocks"] if b["type"] == "context" for e in b["elements"])
    assert "Bids" in context and "Interviews" in context
    assert len(view["blocks"]) <= 100


async def test_view_for_submitted_day_says_edit(me):
    (review, contact), (goal,) = await week(me)
    us = await Account(name="US Main", createdBy=me.id, region="US").insert()
    await put_day("2026-11-10", me, {
        "results": {review: {"done": True}, contact: {"value": 31}},
        "done": {"bidsSelfByProfile": {str(us.id): 40}}, "notes": "Onboarding 4h"})
    view = await build_view(me, TUE)
    assert view["title"]["text"] == "Edit today's update"
    blocks = by_id(view)
    assert blocks[f"item:{review}"]["element"]["initial_options"]
    assert blocks[f"item:{contact}"]["element"]["initial_value"] == "31"
    assert "initial_value" not in blocks[f"item:{goal}"]["element"]
    assert blocks[f"self:{us.id}"]["element"]["initial_value"] == "40"
    assert blocks["notes"]["element"]["initial_value"] == "Onboarding 4h"


async def test_unlinked_user_gets_ephemeral(me, slack):
    await process_standup_click("U0NOBODY1", "trig", "2026-11-10", RESPONSE_URL)
    assert slack.modals == [] and len(slack.replies) == 1 and "Profile" in slack.replies[0]
    # Linked: the form opens, on a weekend day too (extra work).
    await week(me)
    await process_standup_click(SLACK_ID, "trig", SAT.isoformat(), RESPONSE_URL)
    assert len(slack.modals) == 1 and json.loads(slack.modals[0][1]["private_metadata"]) == {"day": "2026-11-14"}
    assert slack.calls[0][0] == "views.open" and slack.calls[0][1]["trigger_id"] == "trig"
    # A malformed or future day is refused.
    await process_standup_click(SLACK_ID, "trig", "nope", RESPONSE_URL)
    await process_standup_click(SLACK_ID, "trig", "2026-11-20", RESPONSE_URL)
    assert len(slack.modals) == 1 and len(slack.replies) == 3


async def test_submission_saves_and_posts_once(me, db, slack):
    (review, contact), (goal,) = await week(me)
    us = await Account(name="US Main", createdBy=me.id, region="US").insert()
    view = await build_view(me, TUE)
    submitted = state(view, **{f"item:{review}": True, f"item:{contact}": "31", f"self:{us.id}": "40",
                               "notes": "Onboarding 4h"})
    assert submission_errors(submitted) == {}
    body = read_submission(submitted)
    # Every item the form showed has a result, untouched ones included.
    assert set(body.results) == {review, contact, goal}
    await process_standup_submission(submitted, SLACK_ID)
    plan = await stored(me, TUE)
    assert plan.submittedAt is not None and plan.notes == "Onboarding 4h"
    assert plan.results[review].done is True and plan.results[contact].value == 31
    assert plan.results[contact].target == 50 and plan.results[goal].target == 250  # frozen, untouched kept
    assert plan.results[goal].value is None and plan.results[goal].done is False
    assert plan.done.bidsSelfByProfile == {str(us.id): 40} and plan.done.bidsSelfUs == 40
    assert db == [None]
    await process_standup_submission(submitted, SLACK_ID)  # editing posts nothing new
    assert db == [None, "ts1"]
    assert await DailyPlan.find({"userId": me.id}).count() == 1
    assert slack.told == []


async def test_slack_then_app_edits_same_post(me, db, slack):
    (review, contact), _ = await week(me)
    view = await build_view(me, TUE)
    await process_standup_submission(state(view, **{f"item:{review}": True}), SLACK_ID)
    await put_day("2026-11-10", me, {"results": {review: {"done": True}, contact: {"value": 12}}, "notes": "app"})
    assert db == [None, "ts1"]
    plan = await stored(me, TUE)
    assert plan.slackTs == "ts1" and plan.notes == "app"


async def test_bad_number_returns_field_error(me):
    (review, contact), (goal,) = await week(me)
    us = await Account(name="US Main", createdBy=me.id, region="US").insert()
    view = await build_view(me, TUE)
    assert submission_errors(state(view, **{f"item:{contact}": "-3"})).keys() == {f"item:{contact}"}
    assert submission_errors(state(view, **{f"item:{contact}": "abc"})).keys() == {f"item:{contact}"}
    assert submission_errors(state(view, **{f"item:{goal}": "100001"})).keys() == {f"item:{goal}"}
    assert submission_errors(state(view, **{f"item:{goal}": "1.5"})) == {}  # hours and counts take decimals
    assert submission_errors(state(view, **{f"self:{us.id}": "1.5"})).keys() == {f"self:{us.id}"}
    assert submission_errors(state(view, notes="x" * 2001)).keys() == {"notes"}
    assert submission_errors({}) == {} and submission_errors({"private_metadata": "junk"}) == {}  # never raises
    with pytest.raises(ValueError):
        read_submission({"private_metadata": "junk"})


async def test_view_block_limit(me, slack):
    routines, goals = await week(
        me,
        routines=[{"text": f"Routine {n:02}", "kind": "count", "target": 5} for n in range(20)],
        goals=[{"text": f"Goal {n:02}", "kind": "count", "target": 5} for n in range(20)])
    profiles = [await Account(name=f"P{n:02}", createdBy=me.id, region="US").insert() for n in range(70)]
    last_goal, last_profile = goals[-1], str(profiles[-1].id)
    await put_day("2026-11-10", me, {
        "results": {last_goal: {"value": 3}}, "done": {"bidsSelfByProfile": {last_profile: 7}}})
    view = await build_view(me, TUE)
    assert len(view["blocks"]) <= 100
    ids = by_id(view)
    assert f"item:{last_goal}" not in ids and f"self:{last_profile}" in ids
    assert "open the app" in json.dumps(view["blocks"][-2:])
    # Values the form could not show survive its submission.
    submitted = state(view)
    del submitted["state"]["values"][f"self:{last_profile}"]  # as if the profile was past the limit too
    await process_standup_submission(submitted, SLACK_ID)
    plan = await stored(me, TUE)
    assert plan.results[last_goal].value == 3 and plan.done.bidsSelfByProfile[last_profile] == 7
    shown = {k.split(":", 1)[1] for k in ids if k.startswith("item:")}
    assert set(plan.results) == shown | {last_goal} and shown <= set(routines) | set(goals)


async def test_click_failure_after_open_shows_error_in_modal(me, slack, monkeypatch):
    async def broken(user, day):
        raise RuntimeError("db down")

    monkeypatch.setattr("app.standup.modal.build_view", broken)
    await process_standup_click(SLACK_ID, "trig", "2026-11-10", RESPONSE_URL)
    (view_id, view), = slack.modals
    assert view_id == "V1" and "Something went wrong" in json.dumps(view) and "submit" not in view


async def test_closed_loading_view_is_log_only(me, slack, monkeypatch):
    async def api(method, body):
        if method == "views.update":
            raise RuntimeError("not_found")  # the person closed the loading view
        return {"ok": True, "view": {"id": "V1"}}

    monkeypatch.setattr(slack_bot, "_slack_api", api)
    await process_standup_click(SLACK_ID, "trig", "2026-11-10", RESPONSE_URL)
    assert slack.replies == []


async def test_count_done_is_computed_by_the_server_same_from_app_and_slack(me, db, slack):
    (review, contact), (goal,) = await week(me)
    # Unticked counts below target are not done, from the app or the Slack form.
    await put_day("2026-11-10", me, {"results": {review: {"done": True}, contact: {"value": 50, "done": False},
                                                 goal: {"value": 10, "done": False}}})
    app = (await stored(me, TUE)).results
    await DailyPlan.find_one({"userId": me.id}).delete()
    view = await build_view(me, TUE)
    await process_standup_submission(state(view, **{f"item:{review}": True, f"item:{contact}": "50",
                                                    f"item:{goal}": "10"}), SLACK_ID)
    form = (await stored(me, TUE)).results
    assert app == form
    assert form[contact].done is True and form[contact].value == 50  # at its target
    assert form[goal].done is False  # 10 of 250
    assert form[review].done is True  # check items keep the sent done


async def test_count_without_target_done_when_value_positive(me, db):
    (_r, _c), (goal,) = await week(me, goals=[{"text": "Calls", "kind": "count"}])
    await put_day("2026-11-10", me, {"results": {goal: {"value": 3}}})
    assert (await stored(me, TUE)).results[goal].done is True
    await put_day("2026-11-10", me, {"results": {goal: {"value": 0, "done": False}}})
    assert (await stored(me, TUE)).results[goal].done is False


async def test_migrated_done_without_value_survives_a_resave(me, db, slack):
    (review, contact), _ = await week(me)
    await put_day("2026-11-10", me, {"results": {contact: {"value": 1}}})
    await DailyPlan.get_motor_collection().update_one(
        {"userId": me.id}, {"$set": {f"results.{contact}": {"done": True, "value": None, "target": None}}})
    view = await build_view(me, TUE)
    await process_standup_submission(state(view, **{f"item:{review}": True}), SLACK_ID)
    assert (await stored(me, TUE)).results[contact].done is True


async def test_archived_item_with_result_is_read_only_and_kept(me, db, slack):
    (review, contact), (goal,) = await week(me)
    await put_day("2026-11-10", me, {"results": {contact: {"value": 12}}})
    await put_week(WEEK, me, {"goal": {"bidsSelf": 100, "bidsBidder": 50},
                              "routines": [{"id": review, "text": "Bidder review"}],
                              "weeklyGoals": [{"id": goal, "text": "Email outreach", "kind": "count", "target": 250}],
                              "recapNotes": ""})
    view = await build_view(me, TUE)
    assert f"item:{contact}" not in by_id(view)
    assert "Citizen contact (removed)" in json.dumps(view["blocks"])
    await process_standup_submission(state(view), SLACK_ID)
    assert (await stored(me, TUE)).results[contact].value == 12


async def test_form_shows_log_as_multiline(me):
    (rid,), _ = await week(me, routines=[{"text": "Profile review", "kind": "log"}])
    john = await Account(name="John D.", createdBy=me.id, region="US").insert()
    el = by_id(await build_view(me, TUE))[f"item:{rid}"]
    assert el["optional"] is True and el["hint"]["text"] == "One line per profile: Profile name: what changed"
    assert el["element"]["type"] == "plain_text_input" and el["element"]["multiline"] is True
    assert el["element"]["action_id"] == "log" and "initial_value" not in el["element"]
    await put_day("2026-11-10", me, {"results": {rid: {"entries": [
        {"profileId": str(john.id), "note": "Rewrote summary"}, {"profileId": None, "note": "free line"}]}}})
    el = by_id(await build_view(me, TUE))[f"item:{rid}"]
    assert el["element"]["initial_value"] == "John D.: Rewrote summary\nfree line"


def _log_view(text):
    return {"private_metadata": json.dumps({"day": "2026-11-10"}),
            "state": {"values": {"item:r1": {"log": {"type": "plain_text_input", "value": text}}}}}


def test_form_roundtrip_log():
    r = read_submission(_log_view("John: a\n\n  Maria: b  "))
    assert [e.note for e in r.results["r1"].entries] == ["John: a", "Maria: b"]
    assert all(e.profileId is None for e in r.results["r1"].entries)
    assert read_submission(_log_view("")).results["r1"].entries == []
    assert read_submission(_log_view(None)).results["r1"].entries == []


async def test_keep_unshown_keeps_entries(me):
    from app.standup.modal import _keep_unshown
    from app.plan_save import DayIn
    (rid,), _ = await week(me, routines=[{"text": "Profile review", "kind": "log"}])
    await put_day("2026-11-10", me, {"results": {rid: {"entries": [{"note": "kept"}]}}})
    out = await _keep_unshown(me, TUE, DayIn())
    assert [e.note for e in out.results[rid].entries] == ["kept"]


def test_log_line_too_long_is_a_field_error():
    assert submission_errors(_log_view("x" * 201)) == {"item:r1": "Each line is limited to 200 characters"}
    assert submission_errors(_log_view("\n".join("a" * 5 for _ in range(21)))) == {"item:r1": "At most 20 lines"}
    assert submission_errors(_log_view("ok\n\n" * 20)) == {}


async def test_log_prefill_roundtrip_keeps_profile_link(me, db, slack):
    (rid,), _ = await week(me, routines=[{"text": "Profile review", "kind": "log"}])
    john = await Account(name="John D.", createdBy=me.id, region="US").insert()
    note = "n" * 200
    await put_day("2026-11-10", me, {"results": {rid: {"entries": [
        {"profileId": str(john.id), "note": note}, {"note": "free"}]}}})
    view = await build_view(me, TUE)
    assert json.loads(view["private_metadata"])["logs"] == {rid: {"John D.": str(john.id)}}
    sub = state(view)
    assert submission_errors(sub) == {}  # 200-char note + "John D.: " prefix is fine
    got = read_submission(sub).results[rid].entries
    assert [(e.profileId, e.note) for e in got] == [(str(john.id), note), (None, "free")]
    typed = state(view, **{f"item:{rid}": "John D.: a\nMaria: b"})
    got = read_submission(typed).results[rid].entries
    assert [(e.profileId, e.note) for e in got] == [(str(john.id), "a"), (None, "Maria: b")]


def test_log_old_metadata_without_logs_still_reads():
    assert read_submission(_log_view("A: x")).results["r1"].entries[0].profileId is None


async def test_log_too_long_to_prefill_is_read_only_and_kept(me, db, slack):
    (rid,), _ = await week(me, routines=[{"text": "Profile review", "kind": "log"}])
    await put_day("2026-11-10", me, {"results": {rid: {"entries": [{"note": "x" * 200}] * 20}}})
    view = await build_view(me, TUE)
    assert f"item:{rid}" not in by_id(view)
    assert "Profile review · 20 profiles — edit them in the app" in json.dumps(view["blocks"], ensure_ascii=False)
    await process_standup_submission(state(view), SLACK_ID)
    assert len((await stored(me, TUE)).results[rid].entries) == 20


async def test_private_metadata_stays_under_slacks_cap(me, db, slack):
    (a, b, c), _ = await week(me, routines=[{"text": t, "kind": "log"} for t in "ABC"])
    accounts = [await Account(name=f"Profile {i:02d} " + "x" * 50, createdBy=me.id, region="US").insert()
                for i in range(20)]
    entries = [{"profileId": str(x.id), "note": "n"} for x in accounts]
    await put_day("2026-11-10", me, {"results": {i: {"entries": entries} for i in (a, b, c)}})
    view = await build_view(me, TUE)
    kept = json.loads(view["private_metadata"]).get("logs", {})
    assert len(view["private_metadata"]) <= 3000 and len(kept) < 3
    assert _day(view) == TUE
    dropped = next(i for i in (a, b, c) if i not in kept)
    got = read_submission(state(view)).results[dropped].entries
    assert len(got) == 20 and all(e.profileId is None for e in got)
