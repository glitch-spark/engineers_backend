"""Unit tests for Slack webhook helper (no real network)."""
import os
from unittest.mock import AsyncMock, MagicMock, patch

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

import httpx
import pytest

from app.config import get_settings, reload_settings
from app import slack as slack_mod


@pytest.fixture(autouse=True)
def _clear_settings():
    reload_settings()
    yield
    reload_settings()


@pytest.mark.asyncio
async def test_post_to_slack_skips_when_url_unset(monkeypatch):
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "")
    reload_settings()
    ok = await slack_mod._post_to_slack({"text": "hi"})
    assert ok is False


@pytest.mark.asyncio
async def test_post_to_slack_success():
    reload_settings()
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.text = "ok"

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch("app.slack.httpx.AsyncClient", return_value=mock_client):
        with patch.object(get_settings(), "SLACK_WEBHOOK_URL", "https://hooks.slack.com/test"):
            ok = await slack_mod._post_to_slack({"text": "hello"})

    assert ok is True
    mock_client.post.assert_awaited_once()


@pytest.mark.asyncio
async def test_post_to_slack_logs_rejection_on_bad_status():
    mock_resp = MagicMock()
    mock_resp.status_code = 404
    mock_resp.text = "no_active_hooks"

    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch("app.slack.httpx.AsyncClient", return_value=mock_client):
        with patch.object(get_settings(), "SLACK_WEBHOOK_URL", "https://hooks.slack.com/test"):
            ok = await slack_mod._post_to_slack({"text": "hello"})

    assert ok is False


@pytest.mark.asyncio
async def test_post_to_slack_network_error():
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(side_effect=httpx.ConnectError("offline"))
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch("app.slack.httpx.AsyncClient", return_value=mock_client):
        with patch.object(get_settings(), "SLACK_WEBHOOK_URL", "https://hooks.slack.com/test"):
            ok = await slack_mod._post_to_slack({"text": "hello"})

    assert ok is False


@pytest.mark.asyncio
async def test_notify_new_transaction_uses_frontend_link():
    mock_resp = MagicMock(status_code=200, text="ok")
    mock_client = AsyncMock()
    mock_client.post = AsyncMock(return_value=mock_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)

    with patch("app.slack.httpx.AsyncClient", return_value=mock_client):
        with patch.object(get_settings(), "SLACK_WEBHOOK_URL", "https://hooks.slack.com/test"):
            with patch.object(get_settings(), "FRONTEND_ORIGIN", "http://localhost:3000"):
                await slack_mod.notify_new_transaction(
                    creator_email="a@b.com",
                    amount=10.0,
                    date="2026-07-10",
                    description="test",
                )

    payload = mock_client.post.await_args.kwargs["json"]
    blocks_text = str(payload["blocks"])
    assert "http://localhost:3000/transactions" in blocks_text
    assert '"type": "actions"' not in blocks_text
