"""Bids: Job Applies tailored resumes count only once the job is marked applied with them."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app import alerts_service
from app.models import ResumeApplication
from app.routers import metrics


async def _three_jobs(user):
    for counts in (None, True, False):  # Generator page / applied tailored / not-yet-applied tailored
        await ResumeApplication(userId=user.id, companyName="Acme", companyNameLower="acme", jobDescription="jd",
                                status="completed", countsAsBid=counts,
                                source="generator" if counts is None else "job_applies").insert()


async def test_unapplied_tailored_jobs_are_not_bids(client, staff_user, auth_header):
    await _three_jobs(staff_user)
    start, end = datetime.utcnow() - timedelta(days=1), datetime.utcnow() + timedelta(days=1)
    assert await metrics._bid_count(staff_user.id, start, end) == 2
    assert (await metrics._bids_by_user([staff_user.id], start, end))[staff_user.id] == 2
    feed = (await client.get("/metrics/dashboard-feed", headers=auth_header(staff_user))).json()
    assert sum(1 for e in feed["recent"] if e["kind"] == "bid") == 2


async def test_daily_pace_alert_ignores_unapplied_tailored(staff_user, monkeypatch):
    await _three_jobs(staff_user)
    plan = SimpleNamespace(goal=SimpleNamespace(bidsSelf=3, bidsBidder=0))
    sent = []

    async def plans(ids, monday):
        return {staff_user.id: plan}

    async def put(**kw):
        sent.append(kw["meta"]["count"])
        return True

    monkeypatch.setattr(alerts_service, "weekly_plans_for", plans)
    monkeypatch.setattr(alerts_service, "daily_bids_target", lambda weekly: 2)
    monkeypatch.setattr(alerts_service, "_put_notification", put)
    await alerts_service._tick_daily_bids([staff_user], now=datetime.now(timezone.utc))
    assert sent == [2]
