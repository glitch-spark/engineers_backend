import os
from unittest.mock import patch

import httpx
import pytest

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from app import slack_bot  # noqa: E402
from app.caller_teams import slack as cts  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.models.caller import Caller  # noqa: E402


class FakeClient:
    calls: list = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, **kw):
        FakeClient.calls.append(("POST", url, kw))
        return httpx.Response(200, json={"ok": True, "upload_url": "https://up/x", "file_id": "F1"})

    async def put(self, url, **kw):
        FakeClient.calls.append(("PUT", url, kw))
        return httpx.Response(200)


def _tokens(monkeypatch, main="xoxb-main", callers="xoxb-callers"):
    s = get_settings()
    monkeypatch.setattr(s, "SLACK_BOT_TOKEN", main)
    monkeypatch.setattr(s, "SLACK_CALLERS_BOT_TOKEN", callers)


@pytest.mark.asyncio
async def test_callers_workspace_uses_its_own_token(monkeypatch):
    _tokens(monkeypatch)
    FakeClient.calls = []
    with patch.object(slack_bot.httpx, "AsyncClient", FakeClient):
        await slack_bot._slack_api("chat.postMessage", {"channel": "C"}, workspace="callers")
        await slack_bot._slack_api("chat.postMessage", {"channel": "C"})
    auth = [c[2]["headers"]["Authorization"] for c in FakeClient.calls]
    assert auth == ["Bearer xoxb-callers", "Bearer xoxb-main"]


@pytest.mark.asyncio
async def test_callers_api_raises_when_token_unset(monkeypatch):
    _tokens(monkeypatch, callers=None)
    with pytest.raises(RuntimeError, match="SLACK_CALLERS_BOT_TOKEN is not configured"):
        await cts.api("chat.postMessage", {})


def test_callers_configured_false_for_blank_token(monkeypatch):
    _tokens(monkeypatch, callers="  ")
    assert cts.callers_configured() is False
    _tokens(monkeypatch)
    assert cts.callers_configured() is True


@pytest.mark.asyncio
async def test_upload_files_three_calls(monkeypatch):
    _tokens(monkeypatch)
    FakeClient.calls = []
    completes = []

    async def fake_api(method, body):
        completes.append((method, body))
        return {"ok": True}

    with patch.object(cts.httpx, "AsyncClient", FakeClient), patch.object(cts, "api", fake_api):
        await cts.upload_files("C1", "1.2", [("a.pdf", b"AAA"), ("b.pdf", b"BB")], "hi")
    kinds = [(c[0], c[1].rsplit("/", 1)[-1]) for c in FakeClient.calls]
    assert kinds == [("POST", "files.getUploadURLExternal"), ("PUT", "x"), ("POST", "files.getUploadURLExternal"), ("PUT", "x")]
    assert FakeClient.calls[0][2]["data"] == {"filename": "a.pdf", "length": 3}
    assert FakeClient.calls[1][2]["content"] == b"AAA"
    assert completes == [("files.completeUploadExternal", {
        "channel_id": "C1", "thread_ts": "1.2", "initial_comment": "hi",
        "files": [{"id": "F1", "title": "a.pdf"}, {"id": "F1", "title": "b.pdf"}],
    })]


@pytest.mark.asyncio
async def test_workspace_name(monkeypatch):
    _tokens(monkeypatch)

    async def fake_api(method, body):
        return {"ok": True, "team": "Callers Inc"}

    with patch.object(cts, "api", fake_api):
        assert await cts.workspace_name() == "Callers Inc"
    _tokens(monkeypatch, callers=None)
    assert await cts.workspace_name() is None
