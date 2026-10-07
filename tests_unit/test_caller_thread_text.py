"""Which thread reply a change gets, and the #caller post / reply text (pure, no Slack, no DB)."""

import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from app.interview_caller import caller_thread_event, round_caller_snapshot  # noqa: E402
from app.models.interview import InterviewCaller, InterviewStageEvent  # noqa: E402
from app.slack_bot import caller_round_text, caller_thread_reply, cc_line, slack_time  # noqa: E402

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
    return caller_thread_reply(event, e, prev, owner=owner, managers=list(managers))


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


# --- cc line -----------------------------------------------------------------------------------


def test_cc_line_mentions_each_user_once_in_order():
    assert cc_line(OWNER, MANAGER, OWNER) == "cc <@U0OWNER001> <@U0MANAGER1>"
    assert cc_line(user("Maria"), user("Ana", "U0ANA00001")) == "cc Maria <@U0ANA00001>"


def test_cc_line_skips_missing_users_and_is_none_without_anyone():
    assert cc_line(None, MANAGER) == "cc <@U0MANAGER1>"
    assert cc_line() is None
    assert cc_line(None) is None


# --- main post ------------------------------------------------------------------------------


def test_main_post_owner_first_managers_cc_last():
    lines = caller_round_text(IV, entry(), account=None, owner=OWNER, managers=[MANAGER]).splitlines()
    assert lines[0] == ":telephone_receiver: <@U0OWNER001> — *Caller needed*"
    assert lines[-2:] == [":hourglass_flowing_sand: Not confirmed yet", "cc <@U0MANAGER1>"]
    assert not any(l.startswith(("*Coworkers:*", "*Interview manager:*")) for l in lines)


def test_main_post_detail_lines_in_order():
    lines = caller_round_text(IV, entry(), account=None, owner=OWNER, managers=[MANAGER]).splitlines()
    assert [l.split(":*")[0] for l in lines[1:9]] == [
        "*Profile", "*Company name", "*JD Link", "*Recruiter name", "*Stage", "*Interview time", "*Method",
        "*Caller assigned",
    ]


def test_main_post_confirmed_badge():
    text = caller_round_text(IV, entry(confirmed=True), account=None, owner=OWNER, managers=[MANAGER])
    assert text.splitlines()[-2:] == [":white_check_mark: Confirmed", "cc <@U0MANAGER1>"]


def test_main_post_without_managers_has_no_cc():
    assert "cc " not in caller_round_text(IV, entry(), account=None, owner=OWNER, managers=[])


def test_main_post_cc_names_managers_without_slack_id_and_tags_every_manager():
    text = caller_round_text(IV, entry(), account=None, owner=OWNER, managers=[user("Maria")])
    assert text.splitlines()[-1] == "cc Maria"
    text = caller_round_text(IV, entry(), account=None, owner=OWNER, managers=[MANAGER, user("Ana", "U0MANAGER2")])
    assert text.splitlines()[-1] == "cc <@U0MANAGER1> <@U0MANAGER2>"


def test_main_post_without_owner_says_someone():
    head = caller_round_text(IV, entry(), account=None, owner=None, managers=[]).splitlines()[0]
    assert head == ":telephone_receiver: someone — *Caller needed*"


def test_canceled_and_no_caller_heads():
    assert caller_round_text(IV, entry(status="canceled"), account=None, owner=OWNER, managers=[]).startswith(
        ":x: <@U0OWNER001> — *Canceled* ~Caller needed~")
    assert caller_round_text(IV, entry(caller=False, stage="tech_round_1"), account=None, owner=OWNER, managers=[]
                             ).splitlines()[0] == ":telephone_receiver: <@U0OWNER001> — *No caller needed* for Tech Round 1"


def test_main_post_no_caller_variant():
    text = caller_round_text(IV, entry(caller=False, stage="tech_round_1"), account=None, owner=OWNER,
                             managers=[MANAGER])
    assert [l.split(":*")[0] for l in text.splitlines()[1:5]] == [
        "*Profile", "*Company name", "*Stage", "*Interview time"]
    assert text.splitlines()[-1] == "cc <@U0MANAGER1>"
    assert "Caller assigned" not in text and "Confirmed" not in text


def test_canceled_main_post_has_no_badge_and_strikes_the_time():
    text = caller_round_text(IV, entry(status="canceled"), account=None, owner=OWNER, managers=[MANAGER])
    assert "Not confirmed yet" not in text and ":white_check_mark:" not in text
    assert f"*Interview time:* ~{slack_time(START)}~" in text
    assert text.splitlines()[-1] == "cc <@U0MANAGER1>"


def test_rejected_round_reads_canceled_and_a_disabled_caller_needs_none():
    rejected = caller_round_text(IV, entry(status="rejected"), account=None, owner=OWNER, managers=[])
    assert rejected.splitlines()[0] == ":x: <@U0OWNER001> — *Canceled* ~Caller needed~"
    off = caller_round_text(IV, entry(caller=False), account=None, owner=OWNER, managers=[])
    assert off.splitlines()[0] == ":telephone_receiver: <@U0OWNER001> — *No caller needed* for Intro"


# --- thread replies ----------------------------------------------------------------------------


def test_reply_texts_match_copy():
    t, t2 = slack_time(START), slack_time(LATER)
    first = lambda s: s.splitlines()[0]  # noqa: E731
    who = "<@U0OWNER001>"
    assert first(reply("moved", entry(stage="tech_round_1"))) == f":arrow_right: {who} — *Moved to Tech Round 1* — {t} · needs confirmation"
    assert first(reply("moved", entry(stage="final", caller=False))) == f":arrow_right: {who} — *Moved to Final* — {t} · no caller needed"
    assert first(reply("ended", entry(status="canceled"), snap())) == f":no_entry: {who} — *Canceled* — Intro · {t}"
    assert first(reply("ended", entry(status="rejected"), snap())) == f":no_entry: {who} — *Rejected* — Intro · {t}"
    assert first(reply("back_on", entry(), snap(status="canceled"))) == f":white_check_mark: {who} — *Back on* — Intro · {t} · needs confirmation"
    assert first(reply("rescheduled", entry(at=LATER), snap())) == f":repeat: {who} — *Rescheduled* — {t} → {t2} · needs confirmation"
    assert first(reply("confirmed", entry(confirmed=True), snap())) == f":white_check_mark: {who} — *Confirmed* — Intro · {t} · caller Sam"
    assert first(reply("unconfirmed", entry(), snap(confirmed=True))) == f":warning: {who} — *Confirmation removed* — Intro · {t}"
    assert first(reply("removed", entry(stage="panel"))) == f":wastebasket: {who} — *Round removed* — Panel"


def test_reply_owner_first_cc_managers():
    text = reply("confirmed", entry(confirmed=True), snap())
    assert text.splitlines()[0] == f":white_check_mark: <@U0OWNER001> — *Confirmed* — Intro · {slack_time(START)} · caller Sam"
    assert text.splitlines()[-1] == "cc <@U0MANAGER1>"
    assert reply("confirmed", entry(confirmed=True), snap(), managers=[]).splitlines()[-1].startswith(":white_check_mark:")


def test_details_reply_lists_changes_and_none_without_changes():
    text = reply("details", entry(name="Dana"), snap(name="TBD"))
    assert text.splitlines()[0] == ":pencil2: <@U0OWNER001> — *Caller details updated*"
    assert "Caller assigned: TBD → Dana" in text
    assert text.splitlines()[-1] == "cc <@U0MANAGER1>"
    assert reply("details", entry(), snap()) is None
    assert reply("details", entry(name="Dana"), snap(name="TBD"), managers=[]).splitlines()[-1] == "Caller assigned: TBD → Dana"


def test_reply_cc_line_tags_managers_only():
    text = reply("confirmed", entry(confirmed=True), snap())
    assert text.count("<@U0OWNER001>") == 1  # the owner leads the first line, not the cc line
    assert reply("confirmed", entry(confirmed=True), snap(), managers=[user("Maria")], owner=user("Pedro")).splitlines()[-1] == "cc Maria"
    two = reply("confirmed", entry(confirmed=True), snap(), managers=[MANAGER, user("Ana", "U0MANAGER2")])
    assert two.splitlines()[-1] == "cc <@U0MANAGER1> <@U0MANAGER2>"


def test_reply_without_owner_says_someone_and_unknown_event_has_none():
    assert reply("removed", entry(stage="panel"), owner=None).splitlines()[0] == ":wastebasket: someone — *Round removed* — Panel"
    assert reply("nope", entry()) is None


def test_rescheduled_and_confirmed_reply_says_confirmed():
    t, t2 = slack_time(START), slack_time(LATER)
    first = reply("rescheduled", entry(at=LATER, confirmed=True), snap()).splitlines()[0]
    assert first == f":repeat: <@U0OWNER001> — *Rescheduled* — {t} → {t2} · confirmed, caller Sam"
