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


async def test_cap_blocks_single_enqueue(client, staff_user, auth_header, monkeypatch):
    monkeypatch.setattr(resume_queue, "_daily_cap", lambda: 2)
    await _queued(staff_user, 2)
    acc = await make_account(staff_user)
    r = await client.post("/resume/generate", headers=auth_header(staff_user),
                          json={"accountId": str(acc.id), "company": "Acme", "jobDescription": JD})
    assert r.status_code == 429 and "Daily limit of 2" in r.json()["error"]
    assert await resume_queue.remaining_today(staff_user) == 0


async def test_cap_counts_both_sources(staff_user, monkeypatch):
    monkeypatch.setattr(resume_queue, "_daily_cap", lambda: 2)
    acc = await make_account(staff_user)
    await resume_queue.enqueue_generation(user=staff_user, account=acc, jd=JD, company="A")
    await resume_queue.enqueue_generation(user=staff_user, account=acc, jd=JD, company="B", source="job_applies")
    with pytest.raises(Exception) as exc:
        await resume_queue.enqueue_generation(user=staff_user, account=acc, jd=JD, company="C")
    assert getattr(exc.value, "status_code", None) == 429
    # Yesterday's generations don't count.
    await ResumeApplication.get_motor_collection().update_many({}, {"$set": {"createdAt": datetime.utcnow() - timedelta(days=2)}})
    assert await resume_queue.remaining_today(staff_user) == 2


async def test_job_list_includes_source(client, staff_user, auth_header):
    acc = await make_account(staff_user)
    await resume_queue.enqueue_generation(user=staff_user, account=acc, jd=JD, company="A", source="job_applies")
    jobs = (await client.get("/resume/jobs", headers=auth_header(staff_user))).json()["jobs"]
    assert jobs[0]["source"] == "job_applies"


async def test_runner_writes_refresh_lease_and_keep_queue_fields(staff_user):
    await _queued(staff_user, 1)
    claimed = await resume_queue._claim_one()
    coll = ResumeApplication.get_motor_collection()
    later = datetime.utcnow() + timedelta(minutes=4)
    await coll.update_one({"_id": claimed.id}, {"$set": {"leaseAt": later, "countsAsBid": True}})  # heartbeat / applied
    claimed.step = "uploading"
    await resume_queue.persist_job(claimed)
    doc = await coll.find_one({"_id": claimed.id})
    assert doc["step"] == "uploading" and doc["countsAsBid"] is True and doc["attempts"] == 1
    assert doc["leaseAt"] >= datetime.utcnow() - timedelta(seconds=5)  # not wound back to the claim time
    assert await resume_queue._requeue_stale() == 0


async def test_superseded_runner_does_not_overwrite(staff_user, monkeypatch):
    """If the job was re-claimed (attempts moved on), the old runner's writes are dropped."""
    from app.resume_service import GeneratedResume
    from app.routers import resume as resume_router

    acc = await make_account(staff_user)
    await resume_queue.enqueue_generation(user=staff_user, account=acc, jd=JD, company="Acme")
    claimed = await resume_queue._claim_one()
    coll = ResumeApplication.get_motor_collection()

    async def fake_generate(*a, **k):
        await coll.update_one({"_id": claimed.id}, {"$set": {"attempts": 2}})  # another worker re-claimed it
        return GeneratedResume(pdf_bytes=b"%PDF", s3_key="k", plain_text="x")

    monkeypatch.setattr(resume_router, "generate_resume", fake_generate)
    await resume_router.run_job_doc(claimed)
    doc = await coll.find_one({"_id": claimed.id})
    assert doc["status"] == "in_progress" and doc.get("s3Key") is None  # the stale runner wrote nothing


async def test_generator_jobs_are_claimed_before_job_applies_batches(staff_user):
    acc = await make_account(staff_user)
    batch = await resume_queue.enqueue_generation(user=staff_user, account=acc, jd=JD, company="Batch", source="job_applies")
    await ResumeApplication.get_motor_collection().update_one(
        {"_id": batch.id}, {"$set": {"createdAt": datetime.utcnow() - timedelta(hours=1)}}
    )
    gen = await resume_queue.enqueue_generation(user=staff_user, account=acc, jd=JD, company="Now")
    assert (await resume_queue._claim_one()).id == gen.id
    assert (await resume_queue._claim_one()).id == batch.id


async def test_claims_share_slots_fairly_across_users(staff_user, staff_user_2):
    """A user with a 50-job backlog must not starve a user who just queued one job (same source)."""
    big = await _queued(staff_user, 5)
    for i, j in enumerate(big):  # all older than the other user's job
        await ResumeApplication.get_motor_collection().update_one(
            {"_id": j.id}, {"$set": {"source": "job_applies", "createdAt": datetime.utcnow() - timedelta(hours=2, minutes=-i)}}
        )
    (small,) = await _queued(staff_user_2, 1)
    await ResumeApplication.get_motor_collection().update_one({"_id": small.id}, {"$set": {"source": "job_applies"}})
    first = await resume_queue._claim_one()
    second = await resume_queue._claim_one()
    assert first.id == big[0].id  # oldest, nobody running yet
    assert second.id == small.id  # user 1 already has a slot, so user 2 goes next despite being newer
    third = await resume_queue._claim_one()
    assert third.userId == staff_user.id


async def test_heartbeat_survives_a_failed_write(staff_user, monkeypatch):
    await _queued(staff_user, 1)
    claimed = await resume_queue._claim_one()
    monkeypatch.setattr(resume_queue, "HEARTBEAT_SECONDS", 0.01)
    real = resume_queue._coll
    calls = {"n": 0}

    class Flaky:
        def __init__(self, inner):
            self.inner = inner

        async def update_one(self, *a, **k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("network blip")
            return await self.inner.update_one(*a, **k)

    monkeypatch.setattr(resume_queue, "_coll", lambda: Flaky(real()))
    hb = asyncio.create_task(resume_queue._heartbeat(claimed.id))
    await asyncio.sleep(0.1)
    assert not hb.done() and calls["n"] >= 2
    hb.cancel()


async def test_generate_stores_requested_models(client, staff_user, auth_header, ran, anthropic_key):
    acc = await make_account(staff_user)
    r = await client.post("/resume/generate", headers=auth_header(staff_user), json={
        "accountId": str(acc.id), "company": "Acme", "jobDescription": JD, "generateCoverLetter": True,
        "resumeModel": "anthropic:claude-opus-5-5", "coverLetterModel": "anthropic:claude-fable-5-1",
        "screeningModel": "anthropic:claude-haiku-4-5", "coverLetterHook": "  loves your mission  ",
    })
    assert r.status_code == 202, r.text
    job = await ResumeApplication.get(r.json()["jobId"])
    assert job.resumeModelId == "anthropic:claude-opus-5-5"
    assert job.coverLetterModelId == "anthropic:claude-fable-5-1"
    assert job.screeningModelId == "anthropic:claude-haiku-4-5"
    assert job.coverLetterHook == "loves your mission"


async def test_generate_rejects_unknown_model_with_400(client, staff_user, auth_header, ran, anthropic_key):
    acc = await make_account(staff_user)
    r = await client.post("/resume/generate", headers=auth_header(staff_user), json={
        "accountId": str(acc.id), "company": "Acme", "jobDescription": JD, "resumeModel": "anthropic:nope",
    })
    assert r.status_code == 400 and "anthropic:nope" in r.json()["detail"]
    assert await ResumeApplication.count() == 0


async def test_retry_keeps_requested_models(client, staff_user, auth_header, ran):
    acc = await make_account(staff_user)
    job = ResumeApplication(userId=staff_user.id, accountId=acc.id, companyName="Acme", companyNameLower="acme",
                            jobDescription=JD, status="failed", step="queued", errorMessage="boom",
                            resumeModelId="anthropic:claude-opus-5-5", coverLetterModelId="anthropic:claude-fable-5-1",
                            screeningModelId="anthropic:claude-haiku-4-5", estimatedCostUsd=0.4)
    await job.insert()
    r = await client.post(f"/resume/jobs/{job.id}/retry", headers=auth_header(staff_user))
    assert r.status_code == 202, r.text
    fresh = await ResumeApplication.get(job.id)
    assert fresh.status == "queued" and fresh.estimatedCostUsd is None
    assert (fresh.resumeModelId, fresh.coverLetterModelId, fresh.screeningModelId) == (
        "anthropic:claude-opus-5-5", "anthropic:claude-fable-5-1", "anthropic:claude-haiku-4-5")
