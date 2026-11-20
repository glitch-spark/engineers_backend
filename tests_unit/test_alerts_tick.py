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
        patch("app.applications.sync.run_applications_tick", AsyncMock(return_value={})),
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


@pytest.mark.asyncio
async def test_weekly_plan_notification_links_to_plans():
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from zoneinfo import ZoneInfo

    from app import alerts_service

    user = SimpleNamespace(id="u1", slackTimezone=None)
    put = AsyncMock(return_value=True)
    with (
        patch.object(alerts_service, "_put_notification", put),
        patch.object(alerts_service, "resolve_user_tz", lambda _u: ZoneInfo("UTC")),
        patch.object(alerts_service.WeeklyPlan, "find_one", AsyncMock(return_value=None)),
    ):
        # Monday 2026-11-16, 03:00 UTC, zone pinned to UTC: inside the Monday-morning window.
        created = await alerts_service._tick_weekly_plans(
            [user], now=datetime(2026, 11, 16, 3, 0, tzinfo=timezone.utc)
        )
    assert created == 1
    assert put.await_args.kwargs["href"] == "/plans"


@pytest.mark.asyncio
@pytest.mark.parametrize("result, code", [({"error": "failed"}, 1), ({"error": "timeout"}, 1), ({"rows": 3}, 0),
                                          ({"skipped": "disabled"}, 0)])
async def test_run_cron_ticks_fails_the_run_when_the_applications_sync_reports_an_error(result, code):
    """run_applications_tick logs and returns {"error": ...} instead of raising; the run must still show as failed."""
    with (
        patch("app.slack_bot.run_alert_tick", AsyncMock(return_value={})),
        patch("app.alerts_service.run_alerts_tick", AsyncMock(return_value={})),
        patch("app.bids.reports.run_bid_reports_tick", AsyncMock(return_value={})),
        patch("app.applications.sync.run_applications_tick", AsyncMock(return_value=result)),
    ):
        assert await run_cron_ticks() == code
