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


async def _queued(owner, n=1, **kw):
    acc = await make_account(owner)
    jobs = []
    for i in range(n):
        j = ResumeApplication(userId=owner.id, accountId=acc.id, companyName=f"C{i}", companyNameLower=f"c{i}",
                              jobDescription=JD, status=kw.get("status", "queued"), step="queued",
                              attempts=kw.get("attempts", 0), leaseAt=kw.get("leaseAt"))
        await j.insert()
        jobs.append(j)
    return jobs


async def test_concurrency_limit(staff_user, monkeypatch):
    await _queued(staff_user, 5)
    gate = asyncio.Event()
    started = []

    async def runner(job):
        started.append(str(job.id))
        await gate.wait()

    monkeypatch.setattr(resume_queue, "RUNNER", runner)
    monkeypatch.setattr(resume_queue, "_limit", lambda: 2)
    assert await resume_queue.run_queue_once() == 2
    assert await resume_queue.run_queue_once() == 0  # both slots busy
    await asyncio.sleep(0)
    assert len(started) == 2
    assert await ResumeApplication.find({"status": "in_progress"}).count() == 2
    gate.set()
    await asyncio.gather(*list(resume_queue._running))
    assert await resume_queue.run_queue_once() == 2
    await asyncio.gather(*list(resume_queue._running))


async def test_claim_is_exclusive(staff_user):
    await _queued(staff_user, 1)
    a, b = await asyncio.gather(resume_queue._claim_one(), resume_queue._claim_one())
    assert [x is not None for x in (a, b)].count(True) == 1
    claimed = a or b
    assert claimed.status == "in_progress" and claimed.attempts == 1 and claimed.leaseAt is not None


async def test_stale_lease_requeued_once(staff_user):
    old = datetime.utcnow() - timedelta(minutes=10)
    (retry,) = await _queued(staff_user, 1, status="in_progress", attempts=1, leaseAt=old)
    (give_up,) = await _queued(staff_user, 1, status="in_progress", attempts=3, leaseAt=old)
    (fresh,) = await _queued(staff_user, 1, status="in_progress", attempts=1, leaseAt=datetime.utcnow())
    assert await resume_queue._requeue_stale() == 1
    assert (await ResumeApplication.get(retry.id)).status == "queued"
    failed = await ResumeApplication.get(give_up.id)
    assert failed.status == "failed" and "interrupted" in failed.errorMessage
    assert (await ResumeApplication.get(fresh.id)).status == "in_progress"


async def test_old_records_are_left_alone(staff_user):
    """Stuck jobs from before the queue (no `source`, attempts 0) must not be regenerated in bulk."""
    coll = ResumeApplication.get_motor_collection()
    now = datetime.utcnow()
    base = {"userId": staff_user.id, "companyName": "Old", "companyNameLower": "old", "jobDescription": JD,
            "createdAt": now - timedelta(days=30), "updatedAt": now}
    await coll.insert_one({**base, "status": "queued", "step": "queued"})
    await coll.insert_one({**base, "status": "in_progress", "step": "generating_resume"})
    assert await resume_queue._claim_one() is None
    assert await resume_queue._requeue_stale() == 0
    assert await coll.count_documents({"status": "in_progress"}) == 1
