"""Resume generation queue: enqueue, worker, daily cap (real API on the test DB; the LLM/PDF runner is stubbed)."""
import asyncio
from datetime import datetime, timedelta

import pytest

from app import resume_queue
from app.models import Account, ResumeApplication

JD = "Senior Python engineer. Requirements: Python, Kafka. " * 5


@pytest.fixture
def ran(monkeypatch):
    calls = []

    async def runner(job):
        calls.append(str(job.id))

    monkeypatch.setattr(resume_queue, "RUNNER", runner)
    return calls


async def make_account(owner) -> Account:
    acc = Account(name="Backend", createdBy=owner.id, styleTemplate="<html><body><p>Jane</p></body></html>")
    await acc.insert()
    return acc


async def test_generate_enqueues_without_running(client, staff_user, auth_header, ran):
    acc = await make_account(staff_user)
    r = await client.post("/resume/generate", headers=auth_header(staff_user),
                          json={"accountId": str(acc.id), "company": "Acme", "jobDescription": JD, "promptBody": "be brief"})
    assert r.status_code == 202, r.text
    job = await ResumeApplication.get(r.json()["jobId"])
    assert job.status == "queued" and job.source == "generator" and job.countsAsBid is None
    assert job.promptOverride == "be brief" and job.attempts == 0
    await asyncio.sleep(0)
    assert ran == []  # nothing runs until the queue worker claims it


async def test_retry_keeps_prompt_override(client, staff_user, auth_header, ran):
    acc = await make_account(staff_user)
    job = ResumeApplication(userId=staff_user.id, accountId=acc.id, companyName="Acme", companyNameLower="acme",
                            jobDescription=JD, status="failed", step="queued", promptOverride="X", errorMessage="boom")
    await job.insert()
    r = await client.post(f"/resume/jobs/{job.id}/retry", headers=auth_header(staff_user))
    assert r.status_code == 202, r.text
    fresh = await ResumeApplication.get(job.id)
    assert fresh.status == "queued" and fresh.promptOverride == "X" and fresh.errorMessage is None
    assert ran == []
