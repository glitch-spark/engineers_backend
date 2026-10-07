"""Which thread reply a change gets, and the #caller post / reply text (pure, no Slack, no DB)."""

import asyncio
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from app.interview_caller import caller_thread_event, round_caller_snapshot  # noqa: E402
from app.models.interview import InterviewCaller, InterviewStageEvent  # noqa: E402
from app.slack_bot import caller_round_text, caller_thread_reply, slack_time  # noqa: E402

START = datetime(2026, 10, 7, 15, 0, tzinfo=timezone.utc)
LATER = START + timedelta(days=1)


def entry(*, at=START, status="scheduled", confirmed=False, caller=True, stage="intro", name="Sam"):
    return InterviewStageEvent(
        stage=stage, at=at, scheduledAt=at, status=status, confirmed=confirmed,
        caller=InterviewCaller(enabled=caller, callerName=name, startsAt=at, method="video",
                               methodValue="https://meet.example/x"),
    )


def snap(**kw):
    return round_caller_snapshot(entry(**kw))


def user(name, slack=None):
    return SimpleNamespace(name=name, email=f"{name.lower()}@x.io", slackUserId=slack)


IV = SimpleNamespace(companyName="Acme", jobUrl="https://jobs.example/1", accountId=None)
OWNER, MANAGER = user("Pedro", "U0OWNER001"), user("Maria", "U0MANAGER1")


def reply(event, e, prev=None, *, owner=OWNER, managers=(MANAGER,)):
    return asyncio.run(caller_thread_reply(event, e, prev, owner=owner, managers=list(managers)))


# --- caller_thread_event ------------------------------------------------------------------------


def test_event_moved_wins_when_added():
    assert caller_thread_event(None, snap(), added=True) == "moved"
    assert caller_thread_event(None, None, added=True) == "moved"  # the new round has no caller


def test_event_removed():
    assert caller_thread_event(snap(), None, removed=True) == "removed"


def test_event_ended_and_back_on():
    assert caller_thread_event(snap(), snap(status="canceled")) == "ended"
    assert caller_thread_event(snap(), snap(status="rejected")) == "ended"
    assert caller_thread_event(snap(status="canceled"), snap()) == "back_on"


def test_event_rescheduled_before_confirmed():
    assert caller_thread_event(snap(confirmed=True), snap(at=LATER)) == "rescheduled"
    assert caller_thread_event(snap(), snap(at=LATER, confirmed=True)) == "rescheduled"


def test_event_confirmed_and_unconfirmed():
    assert caller_thread_event(snap(), snap(confirmed=True)) == "confirmed"
    assert caller_thread_event(snap(confirmed=True), snap()) == "unconfirmed"


def test_event_details_for_other_fields():
    assert caller_thread_event(snap(name="TBD"), snap(name="Dana")) == "details"


def test_event_none_when_unchanged_or_caller_toggled():
    assert caller_thread_event(snap(), snap()) is None
    assert caller_thread_event(snap(), None) is None  # caller turned off: the main post says so
    assert caller_thread_event(None, snap()) is None


# --- main post ------------------------------------------------------------------------------


def test_main_post_shows_manager_and_badge():
    text = caller_round_text(IV, entry(), account=None, owner=OWNER, coworkers=[], managers=[MANAGER])
    assert "*Interview manager:* <@U0MANAGER1>" in text
    assert ":hourglass_flowing_sand: Not confirmed yet" in text
    text = caller_round_text(IV, entry(confirmed=True), account=None, owner=OWNER, coworkers=[], managers=[MANAGER])
    assert ":white_check_mark: Confirmed" in text


def test_main_post_without_manager():
    text = caller_round_text(IV, entry(), account=None, owner=OWNER, coworkers=[], managers=[])
    assert "*Interview manager:* no interview manager assigned" in text
    text = caller_round_text(IV, entry(), account=None, owner=OWNER, coworkers=[], managers=[user("Maria")])
    assert "*Interview manager:* Maria" in text


def test_main_post_tags_every_manager():
    text = caller_round_text(IV, entry(), account=None, owner=OWNER, coworkers=[],
                             managers=[MANAGER, user("Ana", "U0MANAGER2")])
    assert "*Interview manager:* <@U0MANAGER1> <@U0MANAGER2>" in text


def test_main_post_no_caller_variant():
    text = caller_round_text(IV, entry(caller=False, stage="tech_round_1"), account=None, owner=OWNER,
                             coworkers=[], managers=[MANAGER])
    assert text.splitlines()[0] == ":telephone_receiver: *No caller needed for Tech Round 1* — posted by <@U0OWNER001>"
    assert "Caller assigned" not in text and "Confirmed" not in text


def test_canceled_main_post_has_no_badge():
    text = caller_round_text(IV, entry(status="canceled"), account=None, owner=OWNER, coworkers=[], managers=[MANAGER])
    assert text.startswith(":x: *CANCELED*") and "Not confirmed yet" not in text


# --- thread replies ----------------------------------------------------------------------------


def test_reply_texts_match_copy():
    t, t2 = slack_time(START), slack_time(LATER)
    first = lambda s: s.splitlines()[0]  # noqa: E731
    assert first(reply("moved", entry(stage="tech_round_1"))) == f":arrow_right: *Moved to Tech Round 1* — {t} · needs confirmation"
    assert first(reply("moved", entry(stage="final", caller=False))) == f":arrow_right: *Moved to Final* — {t} · no caller needed"
    assert first(reply("ended", entry(status="canceled"), snap())) == f":no_entry: *Canceled* — Intro · {t}"
    assert first(reply("ended", entry(status="rejected"), snap())) == f":no_entry: *Rejected* — Intro · {t}"
    assert first(reply("back_on", entry(), snap(status="canceled"))) == f":white_check_mark: *Back on* — Intro · {t} · needs confirmation"
    assert first(reply("rescheduled", entry(at=LATER), snap())) == f":repeat: *Rescheduled* — {t} → {t2} · needs confirmation"
    assert first(reply("confirmed", entry(confirmed=True), snap())) == f":white_check_mark: *Confirmed* — Intro · {t} · caller Sam"
    assert first(reply("unconfirmed", entry(), snap(confirmed=True))) == f":warning: *Confirmation removed* — Intro · {t}"
    assert first(reply("removed", entry(stage="panel"))) == ":wastebasket: *Round removed* — Panel"


def test_details_reply_lists_changes_and_none_without_changes():
    text = reply("details", entry(name="Dana"), snap(name="TBD"))
    assert text.splitlines()[0] == ":pencil2: *Caller details updated* by <@U0OWNER001>"
    assert "Caller assigned: TBD → Dana" in text
    assert reply("details", entry(), snap()) is None


def test_reply_cc_line_tags_manager_and_owner():
    assert reply("confirmed", entry(confirmed=True), snap()).splitlines()[-1] == "cc <@U0MANAGER1> <@U0OWNER001>"
    assert reply("confirmed", entry(confirmed=True), snap(), managers=[user("Maria")], owner=user("Pedro")).splitlines()[-1] == "cc Maria Pedro"
    assert reply("confirmed", entry(confirmed=True), snap(), managers=[]).splitlines()[-1] == "cc no interview manager assigned <@U0OWNER001>"
    two = reply("confirmed", entry(confirmed=True), snap(), managers=[MANAGER, user("Ana", "U0MANAGER2")])
    assert two.splitlines()[-1] == "cc <@U0MANAGER1> <@U0MANAGER2> <@U0OWNER001>"


def test_rescheduled_and_confirmed_reply_says_confirmed():
    t, t2 = slack_time(START), slack_time(LATER)
    first = reply("rescheduled", entry(at=LATER, confirmed=True), snap()).splitlines()[0]
    assert first == f":repeat: *Rescheduled* — {t} → {t2} · confirmed, caller Sam"
