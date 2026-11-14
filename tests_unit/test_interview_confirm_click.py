"""The Confirm button in Slack: a click confirms the round only when the clicker is an active platform user allowed
to confirm (the round's user caller, an interview manager, admin or owner). Slack and the response_url are faked."""

import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from urllib.parse import urlencode

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

import pytest  # noqa: E402
from beanie import PydanticObjectId, init_beanie  # noqa: E402
from mongomock_motor import AsyncMongoMockClient  # noqa: E402

from app.interview_day import actions  # noqa: E402
from app.models import Account, UserExtra  # noqa: E402
from app.models.interview import Interview, InterviewCaller, InterviewStageEvent  # noqa: E402
from app.models.interview_question import InterviewQuestion  # noqa: E402
from app.routers import slack_bot as slack_router  # noqa: E402

pytestmark = [
    pytest.mark.filterwarnings("ignore:datetime.datetime.utcnow:DeprecationWarning"),
    pytest.mark.filterwarnings("ignore:Accessing the .model_fields. attribute on the instance:DeprecationWarning"),
]

START = datetime(2026, 10, 7, 15, 0, tzinfo=timezone.utc)
RESPONSE_URL = "https://hooks.slack.com/actions/T1/1/abc"
REFUSAL = "Only the caller or an interview manager can confirm."
SECRET = "test-signing-secret"
PATH = "/integrations/slack/interactions"


@pytest.fixture
async def db():
    database = AsyncMongoMockClient()["confirm-click-unit"]
    await init_beanie(database=database, document_models=[Interview, UserExtra, Account, InterviewQuestion])
    return database


@pytest.fixture
def slack(db, monkeypatch):
    from app.routers import interviews

    monkeypatch.setattr(interviews, "sync_caller_thread", AsyncMock(return_value=False))
    monkeypatch.setattr(interviews, "refresh_today", AsyncMock(return_value=False))
    replies = []

    async def respond(url, payload):
        replies.append((url, payload))

    with patch.object(actions, "_post_ephemeral", respond):
        yield replies


async def make_user(name="Pedro", role="staff", slack_id=None, active=True) -> UserExtra:
    return await UserExtra(
        name=name, email=f"{name.lower()}@x.io", role=role, isActive=active, slackUserId=slack_id,
    ).insert()


async def make_interview(owner, *, caller_id=None, kind="user", name="Cal") -> Interview:
    caller = InterviewCaller(
        enabled=True, callerId=caller_id, callerName=name, callerKind=kind, startsAt=START, method="video",
    )
    entry = InterviewStageEvent(
        stage="intro", at=START, scheduledAt=START, endsAt=START.replace(hour=16), status="scheduled", caller=caller,
    )
    return await Interview(
        accountId=PydanticObjectId(), createdBy=owner.id, scheduledAt=START, companyName="Acme",
        stageHistory=[entry.model_dump()], stage="intro", status="scheduled",
    ).insert()


def value(iv) -> str:
    return f"{iv.id}:{iv.stageHistory[-1].id}"


async def confirmed(iv) -> bool:
    return (await Interview.get(iv.id)).stageHistory[-1].confirmed


async def test_caller_click_confirms(slack):
    owner = await make_user()
    caller = await make_user("Cal", slack_id="U0CALLER01")
    iv = await make_interview(owner, caller_id=str(caller.id))
    await actions.process_confirm_click(value(iv), "U0CALLER01", RESPONSE_URL)
    assert await confirmed(iv) is True
    assert slack == []


async def test_clicking_again_keeps_it_confirmed(slack):
    owner = await make_user()
    caller = await make_user("Cal", slack_id="U0CALLER01")
    iv = await make_interview(owner, caller_id=str(caller.id))
    await actions.process_confirm_click(value(iv), "U0CALLER01", RESPONSE_URL)
    await actions.process_confirm_click(value(iv), "U0CALLER01", RESPONSE_URL)
    assert await confirmed(iv) is True and slack == []


async def test_interview_manager_click_confirms_external_round(slack):
    owner = await make_user()
    await make_user("Mia", "interview_manager", "U0MANAGER1")
    iv = await make_interview(owner, caller_id="ext1", kind="external", name="Sam")
    await actions.process_confirm_click(value(iv), "U0MANAGER1", RESPONSE_URL)
    assert await confirmed(iv) is True and slack == []


async def test_unknown_slack_user_refused(slack):
    owner = await make_user()
    caller = await make_user("Cal", slack_id="U0CALLER01")
    iv = await make_interview(owner, caller_id=str(caller.id))
    await actions.process_confirm_click(value(iv), "U0STRANGER", RESPONSE_URL)
    assert await confirmed(iv) is False
    assert slack == [(RESPONSE_URL, {"response_type": "ephemeral", "replace_original": False, "text": REFUSAL})]


async def test_blank_slack_user_refused(slack):
    owner = await make_user()
    await make_user("Cal", slack_id=None)  # a user with no Slack id must not match a blank click id
    iv = await make_interview(owner, caller_id="ext1", kind="external")
    await actions.process_confirm_click(value(iv), "", RESPONSE_URL)
    assert await confirmed(iv) is False
    assert [p["text"] for _, p in slack] == [REFUSAL]


async def test_inactive_user_refused(slack):
    owner = await make_user()
    caller = await make_user("Cal", slack_id="U0CALLER01", active=False)
    iv = await make_interview(owner, caller_id=str(caller.id))
    await actions.process_confirm_click(value(iv), "U0CALLER01", RESPONSE_URL)
    assert await confirmed(iv) is False
    assert [p["text"] for _, p in slack] == [REFUSAL]


async def test_other_staff_refused_by_confirm_round(slack):
    owner = await make_user()
    await make_user("Other", slack_id="U0OTHER001")
    iv = await make_interview(owner, caller_id="ext1", kind="external")
    await actions.process_confirm_click(value(iv), "U0OTHER001", RESPONSE_URL)
    assert await confirmed(iv) is False
    assert [p["text"] for _, p in slack] == [REFUSAL]


@pytest.mark.parametrize("bad", ["", "nocolon", ":", "abc:", ":abc"])
async def test_malformed_value_gets_not_found_ephemeral(slack, bad):
    await make_user("Mia", "admin", "U0ADMIN001")
    await actions.process_confirm_click(bad, "U0ADMIN001", RESPONSE_URL)
    assert [p["text"] for _, p in slack] == ["not found"]


async def test_unknown_interview_and_round(slack):
    admin = await make_user("Mia", "admin", "U0ADMIN001")
    iv = await make_interview(admin, caller_id="ext1", kind="external")
    await actions.process_confirm_click(f"{PydanticObjectId()}:{iv.stageHistory[-1].id}", "U0ADMIN001", RESPONSE_URL)
    await actions.process_confirm_click(f"{iv.id}:nope", "U0ADMIN001", RESPONSE_URL)
    assert [p["text"] for _, p in slack] == ["not found", "stage not found"]
    assert await confirmed(iv) is False


async def test_tbd_round_reply_is_the_confirm_error(slack):
    admin = await make_user("Mia", "admin", "U0ADMIN001")
    iv = await make_interview(admin, caller_id=None, kind="external", name="TBD")
    await actions.process_confirm_click(value(iv), "U0ADMIN001", RESPONSE_URL)
    assert [p["text"] for _, p in slack] == ["Assign a caller first"]


async def test_unexpected_error_is_swallowed_with_generic_reply(slack):
    with patch.object(actions, "confirm_round", AsyncMock(side_effect=RuntimeError("boom"))):
        await make_user("Mia", "admin", "U0ADMIN001")
        await actions.process_confirm_click(f"{PydanticObjectId()}:r1", "U0ADMIN001", RESPONSE_URL)
    assert [p["text"] for _, p in slack] == ["Something went wrong. Try again from the app."]


# --- router -----------------------------------------------------------------------------------------


def _signed(body: bytes) -> dict:
    ts = str(int(time.time()))
    sig = "v0=" + hmac.new(SECRET.encode(), f"v0:{ts}:".encode() + body, hashlib.sha256).hexdigest()
    return {
        "X-Slack-Request-Timestamp": ts, "X-Slack-Signature": sig,
        "Content-Type": "application/x-www-form-urlencoded",
    }


def _confirm_body() -> bytes:
    payload = {
        "type": "block_actions", "user": {"id": "U0CALLER01"}, "response_url": RESPONSE_URL,
        "actions": [{"action_id": "interview_confirm", "value": "iv1:r1", "type": "button"}],
    }
    return urlencode({"payload": json.dumps(payload)}).encode()


async def _post(body: bytes, headers: dict, seen):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    app = FastAPI()
    app.include_router(slack_router.router)
    with (
        patch.object(slack_router, "get_settings", lambda: SimpleNamespace(SLACK_SIGNING_SECRET=SECRET)),
        patch.object(slack_router, "process_confirm_click", seen),
    ):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
            return await c.post(PATH, content=body, headers=headers)


async def test_router_dispatches_confirm_action(db):
    seen = AsyncMock()
    body = _confirm_body()
    r = await _post(body, _signed(body), seen)
    assert r.status_code == 200
    seen.assert_awaited_once_with("iv1:r1", "U0CALLER01", RESPONSE_URL)


async def test_router_confirm_needs_a_valid_signature(db):
    seen = AsyncMock()
    body = _confirm_body()
    headers = _signed(body)
    headers["X-Slack-Signature"] = "v0=" + "0" * 64
    r = await _post(body, headers, seen)
    assert r.status_code == 401
    seen.assert_not_awaited()
