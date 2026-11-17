"""Slack interactivity plumbing for the coordination channel: block_actions routing, view_submission handling, and
the click / reply / modal helpers. Slack is never reached: `_slack_api` and `_post_ephemeral` are patched."""

import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import urlencode

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

import pytest  # noqa: E402
from beanie import init_beanie  # noqa: E402
from mongomock_motor import AsyncMongoMockClient  # noqa: E402

from app import slack_bot  # noqa: E402
from app.coordination import actions  # noqa: E402
from app.coordination.actions import FORM_FAILED, NOT_LINKED, TRY_AGAIN, Click  # noqa: E402
from app.models import UserExtra  # noqa: E402
from app.routers import slack_bot as slack_router  # noqa: E402
from tests_unit.test_slack_interactions import PATH, RESPONSE_URL, SECRET, signed  # noqa: E402

pytestmark = pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning")


@pytest.fixture
async def db():
    database = AsyncMongoMockClient()["coord-interactions-unit"]
    await init_beanie(database=database, document_models=[UserExtra])
    return database


@pytest.fixture
def replies():
    sent = []

    async def respond(url, payload):
        sent.append((url, payload))

    with patch.object(actions, "_post_ephemeral", respond):
        yield sent


@pytest.fixture
def client():
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    app = FastAPI()
    app.include_router(slack_router.router)
    settings = lambda: SimpleNamespace(SLACK_SIGNING_SECRET=SECRET)  # noqa: E731
    with patch.object(slack_router, "get_settings", settings):
        yield AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


def block_actions_body(action_id, *, value=None, selected=None, user="U0PUMA0001") -> bytes:
    action = {"action_id": action_id, "type": "button"}
    if value is not None:
        action["value"] = value
    if selected is not None:
        action["selected_option"] = {"value": selected}
    payload = {
        "type": "block_actions", "user": {"id": user}, "response_url": RESPONSE_URL, "trigger_id": "T.1",
        "container": {"channel_id": "C1", "message_ts": "1.2"}, "actions": [action],
    }
    return urlencode({"payload": json.dumps(payload)}).encode()


def view_body(view, user="U0PUMA0001") -> bytes:
    payload = {"type": "view_submission", "user": {"id": user}, "view": view}
    return urlencode({"payload": json.dumps(payload)}).encode()


async def post(client, body):
    return await client.post(PATH, content=body, headers=signed(body))


VIEW = {"callback_id": "coord_reschedule", "private_metadata": "{}", "state": {"values": {}}}


# --- router -------------------------------------------------------------------------------------------


async def test_coord_click_is_acked_and_dispatched(client):
    with patch.object(slack_router, "process_coordination_click", AsyncMock()) as handler:
        res = await post(client, block_actions_body("coord_confirm", value="IV:R"))
    assert res.status_code == 200
    handler.assert_awaited_once_with(Click("coord_confirm", "IV:R", "U0PUMA0001", RESPONSE_URL, "T.1", "C1", "1.2"))


async def test_select_value_comes_from_selected_option(client):
    with patch.object(slack_router, "process_coordination_click", AsyncMock()) as handler:
        res = await post(client, block_actions_body("coord_choose_caller", selected="IV:R:C9"))
    assert res.status_code == 200
    assert handler.await_args.args[0].value == "IV:R:C9"


async def test_url_buttons_are_acked_without_work(client):
    with patch.object(slack_router, "process_coordination_click", AsyncMock()) as handler:
        res = await post(client, block_actions_body("coord_open_brief", value=""))
    assert res.status_code == 200
    assert res.json() == {"ok": True}
    handler.assert_not_awaited()


async def test_view_submission_errors_are_returned_inline(client):
    errors = {"time": "Pick a time in the future."}
    with (
        patch.object(slack_router, "submission_errors", lambda view, now: errors),
        patch.object(slack_router, "process_coordination_submission", AsyncMock()) as handler,
    ):
        res = await post(client, view_body(VIEW))
    assert res.status_code == 200
    assert res.json() == {"response_action": "errors", "errors": errors}
    handler.assert_not_awaited()


async def test_view_submission_ok_closes_the_modal(client):
    with (
        patch.object(slack_router, "submission_errors", lambda view, now: {}),
        patch.object(slack_router, "process_coordination_submission", AsyncMock()) as handler,
    ):
        res = await post(client, view_body(VIEW))
    assert res.status_code == 200
    assert res.content == b""
    handler.assert_awaited_once_with(VIEW, "U0PUMA0001")


async def test_foreign_view_submission_is_ignored(client):
    view = {**VIEW, "callback_id": "something_else"}
    with patch.object(slack_router, "process_coordination_submission", AsyncMock()) as handler:
        res = await post(client, view_body(view))
    assert res.status_code == 200
    handler.assert_not_awaited()


async def test_unsigned_view_submission_is_401(client):
    body = view_body(VIEW)
    headers = signed(body, secret="wrong")
    with patch.object(slack_router, "process_coordination_submission", AsyncMock()) as handler:
        res = await client.post(PATH, content=body, headers=headers)
    assert res.status_code == 401
    handler.assert_not_awaited()


# --- dispatcher ---------------------------------------------------------------------------------------


def a_click(action_id="coord_confirm"):
    return Click(action_id, "IV:R", "U0PUMA0001", RESPONSE_URL, "T.1", "C1", "1.2")


async def test_unlinked_clicker_is_told_to_link(db, replies):
    await actions.process_coordination_click(a_click())
    assert replies == [
        (RESPONSE_URL, {
            "response_type": "ephemeral", "replace_original": False,
            "text": "Link your Slack in Account settings first.",
        }),
    ]
    assert NOT_LINKED == "Link your Slack in Account settings first."


async def test_inactive_user_counts_as_unlinked(db, replies):
    await UserExtra(name="Pu", email="pu@x.io", role="staff", isActive=False, slackUserId="U0PUMA0001").insert()
    await actions.process_coordination_click(a_click())
    assert [p["text"] for _, p in replies] == [NOT_LINKED]


async def test_slack_platform_user_blank_id_is_none(db):
    assert await actions.slack_platform_user("  ") is None


async def test_handler_is_called_with_click_and_user(db, replies, monkeypatch):
    user = await UserExtra(name="Pu", email="pu@x.io", role="staff", slackUserId="U0PUMA0001").insert()
    handler = AsyncMock()
    monkeypatch.setitem(actions.HANDLERS, "coord_confirm", handler)
    await actions.process_coordination_click(a_click())
    handler.assert_awaited_once()
    assert handler.await_args.args[0] == a_click()
    assert handler.await_args.args[1].id == user.id
    assert replies == []


async def test_unknown_action_is_ignored(db, replies):
    await UserExtra(name="Pu", email="pu@x.io", role="staff", slackUserId="U0PUMA0001").insert()
    await actions.process_coordination_click(a_click("coord_nope"))
    assert replies == []


async def test_handler_crash_says_try_again(db, replies, monkeypatch):
    await UserExtra(name="Pu", email="pu@x.io", role="staff", slackUserId="U0PUMA0001").insert()
    monkeypatch.setitem(actions.HANDLERS, "coord_confirm", AsyncMock(side_effect=RuntimeError("boom")))
    await actions.process_coordination_click(a_click())
    assert [p["text"] for _, p in replies] == [TRY_AGAIN]
    assert TRY_AGAIN == "Something went wrong. Try again from the app."


# --- click parsing ------------------------------------------------------------------------------------


def test_click_from_payload_fallbacks():
    click = actions.click_from_payload({
        "user": {"id": "U1"}, "channel": {"id": "C7"}, "message": {"ts": "9.9"},
        "actions": [{"action_id": "coord_confirm", "value": "a:b"}],
    })
    assert click == Click("coord_confirm", "a:b", "U1", "", "", "C7", "9.9")
    assert actions.click_from_payload({}) == Click("", "", "", "", "", "", "")


# --- reply helpers ------------------------------------------------------------------------------------


async def test_reply_and_replace_payloads(replies):
    await actions.reply(RESPONSE_URL, "hi")
    await actions.replace(RESPONSE_URL, "t", [{"type": "divider"}])
    assert replies == [
        (RESPONSE_URL, {"response_type": "ephemeral", "replace_original": False, "text": "hi"}),
        (RESPONSE_URL, {
            "response_type": "in_channel", "replace_original": True, "text": "t", "blocks": [{"type": "divider"}],
        }),
    ]


async def test_open_modal():
    calls = []

    async def api(method, body):
        calls.append((method, body))
        return {"ok": True}

    with patch.object(slack_bot, "_slack_api", api):
        assert await actions.open_modal("T.1", {"type": "modal"}) is True
    assert calls == [("views.open", {"trigger_id": "T.1", "view": {"type": "modal"}})]

    async def boom(method, body):
        raise RuntimeError("expired_trigger_id")

    with patch.object(slack_bot, "_slack_api", boom):
        assert await actions.open_modal("T.1", {"type": "modal"}) is False
    assert FORM_FAILED == "Couldn't open the form — click again."


async def test_tell_posts_ephemeral_and_never_raises():
    calls = []

    async def api(method, body):
        calls.append((method, body))
        return {"ok": True}

    with patch.object(slack_bot, "_slack_api", api):
        await actions.tell("C1", "U1", "hello")
    assert calls == [("chat.postEphemeral", {"channel": "C1", "user": "U1", "text": "hello"})]

    async def boom(method, body):
        raise RuntimeError("channel_not_found")

    with patch.object(slack_bot, "_slack_api", boom):
        await actions.tell("C1", "U1", "hello")
