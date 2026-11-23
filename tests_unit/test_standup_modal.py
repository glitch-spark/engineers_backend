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
from app.routers.daily_plans import _sync_slack as REAL_SYNC_SLACK, put_day
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
    return await UserExtra(name="Ana", email="ana@x.io", plansOwnWeek=True, slackUserId=SLACK_ID).insert()


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


def _pick(value):
    return {"type": "static_select", "selected_option": {"value": value} if value else None}


def _tc_view(text=None, choice="__", note=None, note_sent=True):
    values = {}
    if text is not None:
        values["item:t1"] = {"text": {"type": "plain_text_input", "value": text}}
    if choice != "__":
        values["item:c1"] = {"choice": _pick(choice)}
        if note_sent:
            values["note:c1"] = {"note": {"type": "plain_text_input", "value": note}}
    return {"private_metadata": json.dumps({"day": "2026-11-10"}), "state": {"values": values}}


def test_form_roundtrip_text_and_choice():
    r = read_submission(_tc_view(text="  did it ", choice="Yes", note=" fine "))
    assert r.results["t1"].text == "did it"
    assert (r.results["c1"].choice, r.results["c1"].text) == ("Yes", "fine")


def test_form_clears_text_choice_and_note():
    r = read_submission(_tc_view(text="", choice=None, note=None))
    assert r.results["t1"].text is None and "text" in r.results["t1"].model_fields_set
    assert r.results["c1"].choice is None and "choice" in r.results["c1"].model_fields_set
    assert "text" in r.results["c1"].model_fields_set
    only_note = read_submission(_tc_view(choice=None, note="just a note")).results["c1"]
    assert (only_note.choice, only_note.text) == (None, "just a note")


def test_text_and_note_over_500_are_field_errors():
    assert submission_errors(_tc_view(text="x" * 501, choice="Yes", note="y" * 501)) == {
        "item:t1": "Up to 500 characters", "note:c1": "Up to 500 characters"}
    assert submission_errors(_tc_view(text="x" * 500, choice="Yes", note="y" * 500)) == {}


async def test_view_shows_text_and_choice(me):
    (t, c), _ = await week(me, routines=[{"text": "Mood", "kind": "text"},
                                         {"text": "Blockers?", "kind": "choice", "options": ["Yes", "No", "Maybe"]}])
    await put_day("2026-11-10", me, {"results": {t: {"text": "calm"}, c: {"choice": "No", "text": "later"}}})
    blocks = by_id(await build_view(me, TUE))
    el = blocks[f"item:{t}"]["element"]
    assert (el["type"], el["action_id"], el["multiline"], el["max_length"], el["initial_value"]) == (
        "plain_text_input", "text", True, 500, "calm")
    assert blocks[f"item:{t}"]["optional"] is True
    sel = blocks[f"item:{c}"]["element"]
    assert sel["type"] == "static_select" and sel["action_id"] == "choice" and blocks[f"item:{c}"]["optional"]
    assert [o["value"] for o in sel["options"]] == ["Yes", "No", "Maybe"]
    assert sel["initial_option"]["value"] == "No"
    note = blocks[f"note:{c}"]
    assert note["optional"] is True and note["element"]["action_id"] == "note"
    assert note["element"]["max_length"] == 500 and note["element"]["initial_value"] == "later"


async def test_choice_no_longer_an_option_has_no_initial_option(me):
    (c,), _ = await week(me, routines=[{"text": "Pick", "kind": "choice", "options": ["A", "B"]}])
    await put_day("2026-11-10", me, {"results": {c: {"choice": "A"}}})
    await week(me, routines=[{"id": c, "text": "Pick", "kind": "choice", "options": ["B", "C"]}])
    blocks = by_id(await build_view(me, TUE))
    assert "initial_option" not in blocks[f"item:{c}"]["element"]


async def test_text_choice_roundtrip_saves(me, db, slack):
    (t, c), _ = await week(me, routines=[{"text": "Mood", "kind": "text"},
                                         {"text": "Pick", "kind": "choice", "options": ["A", "B"]}])
    view = await build_view(me, TUE)
    values = {f"item:{t}": {"text": {"type": "plain_text_input", "value": "ok"}},
              f"item:{c}": {"choice": _pick("B")},
              f"note:{c}": {"note": {"type": "plain_text_input", "value": "why"}}}
    sub = {**view, "state": {"values": values}}
    assert submission_errors(sub) == {}
    await process_standup_submission(sub, SLACK_ID)
    plan = await stored(me, TUE)
    assert plan.results[t].text == "ok" and plan.results[c].choice == "B" and plan.results[c].text == "why"
    again = await build_view(me, TUE)
    assert again["blocks"] and by_id(again)[f"item:{c}"]["element"]["initial_option"]["value"] == "B"
    cleared = {**view, "state": {"values": {
        f"item:{t}": {"text": {"value": ""}}, f"item:{c}": {"choice": _pick(None)}, f"note:{c}": {"note": {"value": ""}}}}}
    await process_standup_submission(cleared, SLACK_ID)
    plan = await stored(me, TUE)
    assert plan.results[t].text is None and plan.results[c].choice is None and plan.results[c].text is None


async def test_archived_text_and_choice_show_a_removed_line(me):
    (t, c), _ = await week(me, routines=[{"text": "Mood", "kind": "text"},
                                         {"text": "Pick", "kind": "choice", "options": ["A", "B"]}])
    await put_day("2026-11-10", me, {"results": {t: {"text": "calm"}, c: {"choice": "A", "text": "why"}}})
    await week(me, routines=[{"text": "Other"}])
    ctx = [b["elements"][0]["text"] for b in (await build_view(me, TUE))["blocks"] if b["type"] == "context"]
    assert "Mood (removed) · calm" in ctx and "Pick (removed) · A — why" in ctx


async def test_keep_unshown_keeps_text_and_choice(me):
    from app.plan_save import DayIn
    from app.standup.modal import _keep_unshown
    (t, c), _ = await week(me, routines=[{"text": "Mood", "kind": "text"},
                                         {"text": "Pick", "kind": "choice", "options": ["A", "B"]}])
    await put_day("2026-11-10", me, {"results": {t: {"text": "calm"}, c: {"choice": "A", "text": "why"}}})
    await week(me, routines=[{"id": c, "text": "Pick", "kind": "choice", "options": ["B", "C"]}])  # A no longer an option
    out = await _keep_unshown(me, TUE, DayIn())
    await process_standup_submission({"private_metadata": json.dumps({"day": "2026-11-10"}), "state": {"values": {}}}, SLACK_ID)
    plan = await stored(me, TUE)
    assert "text" not in out.results[t].model_fields_set and "choice" not in out.results[c].model_fields_set
    assert plan.results[t].text == "calm" and plan.results[c].choice == "A" and plan.results[c].text == "why"


async def test_choice_item_uses_two_blocks_in_the_room(me, monkeypatch):
    monkeypatch.setattr("app.standup.modal.MAX_BLOCKS", 12)  # 3 context lines + Extra leave 8: 4 choices fit with the cut line
    items = [{"text": f"Pick {i}", "kind": "choice", "options": ["A", "B"]} for i in range(20)]
    await week(me, routines=items, goals=[{"text": f"G{i}", "kind": "choice", "options": ["A", "B"]} for i in range(20)])
    view = await build_view(me, TUE)
    assert len(view["blocks"]) <= 12
    ids = {b.get("block_id") for b in view["blocks"]}
    for bid in ids:  # a choice never loses its note block
        if bid and bid.startswith("item:"):
            assert "note:" + bid[5:] in ids
    assert any("more lines" in b["elements"][0]["text"] for b in view["blocks"] if b["type"] == "context")


async def _stale_setup(me, extra=()):
    (c,), _ = await week(me, routines=[{"text": "Pick", "kind": "choice", "options": ["A", "B"]}])
    await put_day("2026-11-10", me, {"results": {c: {"choice": "A", "text": "n"}}})
    ids, _ = await week(me, routines=[{"id": c, "text": "Pick", "kind": "choice", "options": ["B", "C"]}, *extra])
    return c, ids[1:]


async def test_stale_choice_is_kept_on_untouched_submit(me, db, slack):
    c, _ = await _stale_setup(me)
    view = await build_view(me, TUE)
    assert json.loads(view["private_metadata"])["stale"] == [c]
    assert by_id(view)[f"item:{c}"]["hint"]["text"] == "Current: A (no longer an option)"
    sub = {**view, "state": {"values": {f"item:{c}": {"choice": _pick(None)},
                                        f"note:{c}": {"note": {"value": "n"}}}}}
    await process_standup_submission(sub, SLACK_ID)
    assert (await stored(me, TUE)).results[c].choice == "A"
    sub["state"]["values"][f"item:{c}"] = {"choice": _pick("C")}
    await process_standup_submission(sub, SLACK_ID)
    assert (await stored(me, TUE)).results[c].choice == "C"


async def test_stale_list_survives_the_metadata_cap(me, db, slack):
    c, (a, b, d) = await _stale_setup(me, [{"text": t, "kind": "log"} for t in "ABC"])
    accounts = [await Account(name=f"Profile {i:02d} " + "x" * 50, createdBy=me.id, region="US").insert()
                for i in range(20)]
    entries = [{"profileId": str(x.id), "note": "n"} for x in accounts]
    await put_day("2026-11-10", me, {"results": {i: {"entries": entries} for i in (a, b, d)} | {c: {}}})
    view = await build_view(me, TUE)
    meta = json.loads(view["private_metadata"])
    assert len(view["private_metadata"]) <= 3000 and len(meta["logs"]) < 3 and meta["stale"] == [c]


async def test_saturday_form_and_post_list_only_routines_run_that_day(me, slack, monkeypatch):
    (weekdays, sat), _ = await week(me, routines=[{"text": "Weekday review", "days": [0, 1, 2, 3, 4]},
                                                  {"text": "Saturday sweep", "days": [5]}])
    view = await build_view(me, SAT)
    assert f"item:{sat}" in by_id(view) and f"item:{weekdays}" not in by_id(view)
    assert f"item:{weekdays}" in by_id(await build_view(me, TUE)) and f"item:{sat}" not in by_id(await build_view(me, TUE))
    posts = []

    async def fake_post(plan, owner, **v):
        posts.append(slack_bot.daily_plan_text(plan, owner, **v))
        return "C1", "ts1"

    monkeypatch.setattr(daily_plans, "_sync_slack", REAL_SYNC_SLACK)
    monkeypatch.setattr(daily_plans, "sync_daily_plan_message", fake_post)
    await process_standup_submission(state(view, **{f"item:{sat}": True}), SLACK_ID)
    (post,) = posts
    assert "Saturday sweep" in post and "Weekday review" not in post
