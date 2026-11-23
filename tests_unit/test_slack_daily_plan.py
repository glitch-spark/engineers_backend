"""Unit tests for the daily plan Slack post (text, post, edit in place, delete)."""

import os
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from app.config import get_settings
from app.models.plan_common import Counts, DoneCounts, ItemResult, PlanItem
from app.slack_bot import (
    _plan_number,
    daily_plan_text,
    delete_daily_plan_message,
    sync_daily_plan_message,
)


DIVIDER = "━" * 30


def _plan(**kw):
    base = dict(
        id="plan1",
        date=datetime(2026, 11, 17),
        done=Counts(bidsSelf=140, bidsBidder=100),
        focus=[],
        notes="",
        slackChannel=None,
        slackTs=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def _owner(slack="U123"):
    return SimpleNamespace(name="Pedro", email="p@x.com", slackUserId=slack)


def _e(text, kind, scope, target=None, result=None, iid=None):
    return {"item": PlanItem(id=iid or text, text=text, kind=kind, target=target), "scope": scope, "result": result}


def _body(text):
    return text.splitlines()[3:-3]  # inside the code block, after the divider


def test_goal_vs_done_layout():
    plan = _plan(focus=["fb", "mail"], notes="Company onboarding: 4h · Discord server: 1.5h")
    done = DoneCounts(bidsSelf=134, bidsSelfUs=91, bidsSelfLatam=43, bidsBidder=144, interviewsSelf=2, interviewsCaller=1)
    targets = Counts(bidsSelf=120, bidsSelfUs=80, bidsSelfLatam=40, bidsBidder=150)
    rounds = {"self": {"tech_round_1": 1, "intro": 1}, "caller": {"intro": 1}}
    items = [
        _e("Bidder review", "check", "routine", result=ItemResult(done=True)),
        _e("Citizen contact", "count", "routine", 50, ItemResult(value=31, target=50)),
        _e("Interview prep", "hours", "routine", 2, ItemResult(value=1.5, target=2)),
        _e("Email outreach", "count", "goal", 250, iid="mail"),
        _e("Company Facebook", "check", "goal", result=ItemResult(done=True), iid="fb"),
        _e("Untouched goal", "count", "goal", 10, iid="x"),
    ]
    progress = {"mail": {"value": 180, "done": False, "target": 250}, "fb": {"value": 0, "done": True, "target": None}}
    text = daily_plan_text(plan, _owner(), done=done, rounds=rounds, targets=targets, items=items,
                           progress=progress, week_interviews=(3, 15))
    lines = text.splitlines()
    assert lines[0] == "🗒️ <@U123> · Daily standup · Tue 11/17"
    assert lines[1:3] == ["```", DIVIDER]
    assert _body(text) == [
        "                     Done / Goal",
        "1️⃣ Bids",
        "   Self              134 / 120  ✅  (US 91/80 · LATAM 43/40)",
        "   Bidder            144 / 150  ⚠️",
        "2️⃣ Interviews",
        "   Self              2   Intro(1) · Tech round 1(1)",
        "   Caller            1   Intro(1)",
        "   Week              3 / 15",
        "3️⃣ Routines",
        "   Bidder review     ✅",
        "   Citizen contact   31 / 50  ⚠️",
        "   Interview prep    1.5h / 2h  ⚠️",
        "4️⃣ Weekly goals (week so far)",
        "   Email outreach    180 / 250",
        "   Company Facebook  ✅ done today",
        "5️⃣ Extra",
        "   Company onboarding: 4h · Discord server: 1.5h",
    ]
    assert lines[-3] == DIVIDER and lines[-2] == "```"
    assert lines[-1].endswith("/plans?day=2026-11-17|Open Daily plan>")


def test_marks():
    done = Counts(bidsSelf=120, bidsBidder=3)
    text = daily_plan_text(_plan(), _owner(), done=done, targets=Counts(bidsSelf=120, bidsBidder=4))
    assert "   Self              120 / 120  ✅" in text
    assert "   Bidder            3 / 4  ⚠️" in text
    text = daily_plan_text(_plan(), _owner(), done=done)  # no target: no mark, no Goal
    assert "   Self              120" in text.splitlines()
    assert "✅" not in text and "⚠️" not in text


def test_hours_format():
    items = [_e("Prep", "hours", "routine", 2, ItemResult(value=1.5, target=2)),
             _e("Study", "hours", "routine", 2, ItemResult(value=2, target=2))]
    text = daily_plan_text(_plan(), _owner(), items=items)
    assert "1.5h / 2h  ⚠️" in text
    assert "   Study             2h / 2h  ✅" in text
    assert "2.0" not in text


def test_only_touched_goals_listed():
    items = [
        _e("In focus", "count", "goal", 10, iid="a"),
        _e("Has result", "count", "goal", 10, ItemResult(value=2), iid="b"),
        _e("Untouched", "count", "goal", 10, ItemResult(), iid="c"),
        _e("Not listed", "count", "goal", 10, iid="d"),
    ]
    progress = {k: {"value": 4, "done": False, "target": 10} for k in "abcd"}
    text = daily_plan_text(_plan(focus=["a"]), _owner(), items=items, progress=progress)
    assert "In focus" in text and "Has result" in text
    assert "Untouched" not in text and "Not listed" not in text


def test_empty_sections_left_out():
    lines = daily_plan_text(_plan(), _owner(), done=Counts(), targets=Counts()).splitlines()
    assert lines[1:-1] == [
        "```", DIVIDER, "                     Done / Goal",
        "1️⃣ Bids", "   Self              0", "   Bidder            0",
        "2️⃣ Interviews", "   Self              0", "   Caller            0",
        DIVIDER, "```",
    ]  # no Week without a target, no Routines / Weekly goals / Extra


def test_other_regions_complete_the_self_total():
    done = DoneCounts(bidsSelf=136, bidsSelfUs=70, bidsSelfEu=40, bidsSelfLatam=20)  # 6 Canada / Asia
    targets = Counts(bidsSelf=150, bidsSelfUs=80, bidsSelfEu=40, bidsSelfLatam=30)
    text = daily_plan_text(_plan(), _owner(), done=done, targets=targets)
    assert "(US 70/80 · EU 40/40 · LATAM 20/30 · Other 6)" in text


def test_interview_stages_follow_pipeline_order():
    rounds = {"self": {"final": 1, "cultural": 2, "tech_round_2": 1, "live_coding": 1, "intro": 1, "panel": 1}}
    text = daily_plan_text(_plan(), _owner(), done=Counts(interviewsSelf=7), rounds=rounds)
    assert "7   Intro(1) · Tech round 2(1) · Live Coding(1) · Hiring Manager(2) · Panel(1) · Final(1)" in text


def test_multiline_note_is_indented_and_escaped():
    lines = daily_plan_text(_plan(notes="Update & more\n(Bid <details>)"), _owner()).splitlines()
    assert lines[-6:-3] == ["3️⃣ Extra", "   Update &amp; more", "   (Bid &lt;details&gt;)"]


def test_backticks_cannot_close_the_code_block():
    items = [_e("Fix ```x```", "check", "routine", result=ItemResult(done=True))]
    text = daily_plan_text(_plan(notes="see `y` and ```z```"), _owner(), items=items)
    assert text.count("```") == 2


def test_heading_names_an_unlinked_owner():
    assert daily_plan_text(_plan(), _owner(None)).splitlines()[0] == "🗒️ Pedro · Daily standup · Tue 11/17"


def test_plan_number_keycaps():
    assert _plan_number(3) == "3️⃣" and _plan_number(10) == "🔟" and _plan_number(11) == "11."


def test_unreported_routine_shows_as_short():
    items = [_e("Review", "check", "routine"), _e("Calls", "count", "routine", 50), _e("Goal", "count", "goal", 5)]
    text = daily_plan_text(_plan(), _owner(), items=items)
    assert "   Review            ❌ not done" in text
    assert "   Calls             0 / 50  ⚠️" in text
    assert "Goal " not in text  # weekly goals still list only when touched


@pytest.mark.asyncio
async def test_sync_skips_when_bot_not_configured():
    api = AsyncMock()
    with (
        patch("app.slack_bot.slack_bot_configured", return_value=False),
        patch("app.slack_bot._slack_api", api),
    ):
        assert await sync_daily_plan_message(_plan(), _owner()) is None
    api.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_posts_new_message_to_channel():
    api = AsyncMock(return_value={"ok": True, "channel": "C1", "ts": "111.222"})
    with (
        patch("app.slack_bot.slack_bot_configured", return_value=True),
        patch.object(get_settings(), "SLACK_DAILY_PLAN_CHANNEL_ID", "C1"),
        patch("app.slack_bot._slack_api", api),
    ):
        assert await sync_daily_plan_message(_plan(), _owner()) == ("C1", "111.222")
    method, body = api.await_args.args
    assert method == "chat.postMessage"
    assert body["channel"] == "C1"


@pytest.mark.asyncio
async def test_sync_skips_new_post_without_channel():
    api = AsyncMock()
    with (
        patch("app.slack_bot.slack_bot_configured", return_value=True),
        patch.object(get_settings(), "SLACK_DAILY_PLAN_CHANNEL_ID", None),
        patch("app.slack_bot._slack_api", api),
    ):
        assert await sync_daily_plan_message(_plan(), _owner()) is None
    api.assert_not_awaited()


@pytest.mark.asyncio
async def test_sync_edits_existing_message_in_place():
    api = AsyncMock(return_value={"ok": True})
    plan = _plan(slackChannel="C1", slackTs="111.222")
    with (
        patch("app.slack_bot.slack_bot_configured", return_value=True),
        patch("app.slack_bot._slack_api", api),
    ):
        assert await sync_daily_plan_message(plan, _owner()) == ("C1", "111.222")
    api.assert_awaited_once()
    method, body = api.await_args.args
    assert method == "chat.update"
    assert body["channel"] == "C1"
    assert body["ts"] == "111.222"


@pytest.mark.asyncio
async def test_sync_reposts_when_old_message_was_deleted():
    api = AsyncMock(
        side_effect=[
            RuntimeError("message_not_found"),
            {"ok": True, "channel": "C1", "ts": "333.444"},
        ]
    )
    plan = _plan(slackChannel="C1", slackTs="111.222")
    with (
        patch("app.slack_bot.slack_bot_configured", return_value=True),
        patch.object(get_settings(), "SLACK_DAILY_PLAN_CHANNEL_ID", "C1"),
        patch("app.slack_bot._slack_api", api),
    ):
        assert await sync_daily_plan_message(plan, _owner()) == ("C1", "333.444")
    assert [c.args[0] for c in api.await_args_list] == ["chat.update", "chat.postMessage"]


@pytest.mark.asyncio
async def test_sync_never_raises_on_other_errors():
    api = AsyncMock(side_effect=RuntimeError("ratelimited"))
    plan = _plan(slackChannel="C1", slackTs="111.222")
    with (
        patch("app.slack_bot.slack_bot_configured", return_value=True),
        patch("app.slack_bot._slack_api", api),
    ):
        assert await sync_daily_plan_message(plan, _owner()) is None
    api.assert_awaited_once()


@pytest.mark.asyncio
async def test_delete_removes_post():
    api = AsyncMock(return_value={"ok": True})
    with (
        patch("app.slack_bot.slack_bot_configured", return_value=True),
        patch("app.slack_bot._slack_api", api),
    ):
        await delete_daily_plan_message(_plan(slackChannel="C1", slackTs="111.222"))
    api.assert_awaited_once_with("chat.delete", {"channel": "C1", "ts": "111.222"})


@pytest.mark.asyncio
async def test_delete_without_post_is_noop():
    api = AsyncMock()
    with (
        patch("app.slack_bot.slack_bot_configured", return_value=True),
        patch("app.slack_bot._slack_api", api),
    ):
        await delete_daily_plan_message(_plan())
    api.assert_not_awaited()


@pytest.mark.asyncio
async def test_no_post_until_submitted():
    from app.routers import daily_plans

    plan = SimpleNamespace(submittedAt=None, slackChannel=None, slackTs=None)
    with patch.object(daily_plans, "sync_daily_plan_message", AsyncMock()) as sync:
        await daily_plans._sync_slack(plan, _owner(), Counts(), {})
    sync.assert_not_awaited()


def test_done_count_without_value_shows_done():
    """A migrated ticked line with no number to carry over: done, no value."""
    items = [_e("Calls", "count", "routine", None, ItemResult(done=True)),
             _e("Prep", "hours", "routine", 2, ItemResult(done=True)),
             _e("Mail", "count", "goal", None, ItemResult(done=True), iid="m")]
    text = daily_plan_text(_plan(), _owner(), items=items, progress={"m": {"value": 0, "done": True, "target": None}})
    assert "   Calls             ✅" in text
    assert "   Prep              ✅" in text
    assert "   Mail              ✅ done today" in text


def test_ticked_routine_below_target_shows_done_not_warning():
    items = [_e("Calls", "count", "routine", 50, ItemResult(done=True, value=31))]
    text = daily_plan_text(_plan(), _owner(), items=items)
    assert "   Calls             31 / 50  ✅" in text and "⚠️" not in text


def test_done_weekly_count_goal_shows_done_mark():
    items = [_e("Mail", "count", "goal", 250, ItemResult(value=10), iid="m")]
    text = daily_plan_text(_plan(), _owner(), items=items, progress={"m": {"value": 140, "done": True, "target": 250}})
    assert "   Mail              140 / 250  ✅" in text


def _log(entries):
    from app.models.plan_common import LogEntry
    result = ItemResult(done=True, entries=[LogEntry(**e) for e in entries]) if entries else None
    return _e("Profile review", "log", "routine", result=result)


def test_log_routine_lists_entries():
    items = [_log([{"profileId": "P1", "note": "Rewrote summary"}, {"profileId": None, "note": "Maria: new headline"}])]
    body = _body(daily_plan_text(_plan(), _owner(), items=items, profile_names={"P1": "John D."}))
    i = next(n for n, ln in enumerate(body) if "Profile review" in ln)
    assert body[i].endswith("✅ 2 profiles")
    assert body[i + 1] == "     • John D.: Rewrote summary"
    assert body[i + 2] == "     • Maria: new headline"


def test_log_routine_without_entries_is_not_done():
    body = _body(daily_plan_text(_plan(), _owner(), items=[_log(None)]))
    assert any("Profile review" in ln and ln.endswith("❌ not done") for ln in body)


def test_log_one_entry_is_singular():
    body = _body(daily_plan_text(_plan(), _owner(), items=[_log([{"note": "x"}])]))
    assert any(ln.endswith("✅ 1 profile") for ln in body)


def _tc(kind, scope, **result):
    item = PlanItem(id="x", text="Q", kind=kind, options=["Yes", "No"] if kind == "choice" else [])
    return {"item": item, "scope": scope, "result": ItemResult(**result) if result else None}


def _line(item):
    body = _body(daily_plan_text(_plan(), _owner(), items=[item]))
    return next(ln for ln in body if ln.lstrip().startswith("Q"))


def test_text_routine_line():
    assert _line(_tc("text", "routine", text="Reviewed two <PRs>")).endswith("✅ Reviewed two &lt;PRs&gt;")
    assert _line(_tc("text", "routine", text="x" * 300)).endswith("✅ " + "x" * 199 + "…")
    assert _line(_tc("text", "routine")).endswith("❌ not done")


def test_choice_routine_line():
    assert _line(_tc("choice", "routine", choice="Yes")).endswith("Yes")
    assert _line(_tc("choice", "routine", choice="Yes", text="all good")).endswith("Yes — all good")
    assert _line(_tc("choice", "routine")).endswith("❌ not done")


def test_text_and_choice_goals():
    assert _line(_tc("text", "goal", text="shipped", done=True)).endswith("✅ shipped")
    assert _line(_tc("choice", "goal", choice="No", text="blocked", done=True)).endswith("No — blocked")
    for kind in ("text", "choice"):  # not answered today: only listed when in focus, as "in focus"
        item = _tc(kind, "goal")
        item["item"].id = "q"
        body = _body(daily_plan_text(_plan(focus=["q"]), _owner(), items=[item]))
        assert next(ln for ln in body if ln.lstrip().startswith("Q")).endswith("in focus")
