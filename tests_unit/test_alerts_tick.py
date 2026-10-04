"""Unit tests for the combined Railway cron entrypoint."""

import os
from unittest.mock import AsyncMock, patch

import pytest

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from app.alerts_tick import run_cron_ticks


@pytest.mark.asyncio
async def test_run_cron_ticks_runs_slack_before_alerts():
    order: list[str] = []

    async def slack():
        order.append("slack")
        return {"users": 1, "digest": 1, "skipped": 0}

    async def alerts():
        order.append("alerts")
        return {"card": 0}

    async def bids():
        order.append("bids")
        return {"daily": "not-due", "weekly": "not-due"}

    with (
        patch("app.slack_bot.run_alert_tick", slack),
        patch("app.alerts_service.run_alerts_tick", alerts),
        patch("app.bids.reports.run_bid_reports_tick", bids),
    ):
        assert await run_cron_ticks() == 0
    assert order == ["slack", "alerts", "bids"]


@pytest.mark.asyncio
async def test_run_cron_ticks_still_runs_alerts_if_slack_fails():
    alerts = AsyncMock(return_value={"card": 1})
    with (
        patch("app.slack_bot.run_alert_tick", AsyncMock(side_effect=RuntimeError("dm"))),
        patch("app.alerts_service.run_alerts_tick", alerts),
        patch("app.bids.reports.run_bid_reports_tick", AsyncMock(return_value={})),
    ):
        assert await run_cron_ticks() == 1
    alerts.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_cron_ticks_reports_bid_failure_after_other_steps():
    slack = AsyncMock(return_value={})
    alerts = AsyncMock(return_value={})
    with (
        patch("app.slack_bot.run_alert_tick", slack),
        patch("app.alerts_service.run_alerts_tick", alerts),
        patch("app.bids.reports.run_bid_reports_tick", AsyncMock(side_effect=RuntimeError("s3"))),
    ):
        assert await run_cron_ticks() == 1
    slack.assert_awaited_once()
    alerts.assert_awaited_once()
