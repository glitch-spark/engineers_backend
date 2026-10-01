"""Persistent queue for resume generation, shared by the Generator page and Job Applies.

`enqueue_generation` records a ResumeApplication as `queued`; the worker (see `queue_forever`) claims queued
jobs with bounded concurrency and runs the existing steps (`app.routers.resume.run_job_doc`).
"""
import asyncio
import logging
import os
import time
from datetime import datetime, timedelta
from typing import Literal, Optional

from beanie import PydanticObjectId
from fastapi import HTTPException, status
from pymongo import ReturnDocument

from app.config import get_settings
from app.models import Account, ResumeApplication, UserExtra

log = logging.getLogger(__name__)

HEARTBEAT_SECONDS = 30
STALE_AFTER = timedelta(minutes=3)
SWEEP_SECONDS = 120
POLL_SECONDS = 5
MAX_ATTEMPTS = 3

MAX_JD_BYTES = 50_000
MAX_PROMPT_CHARS = 30_000

_wake = asyncio.Event()


async def _default_runner(job: ResumeApplication) -> None:
    from app.routers.resume import run_job_doc  # router imports this module; resolve lazily

    await run_job_doc(job)


# Runs one claimed job end to end; tests replace it.
RUNNER = _default_runner


def wake() -> None:
    """Tell the worker there is new work (it also polls)."""
    _wake.set()


def _safe_segment(s: str) -> str:
    import re

    return re.sub(r"[^A-Za-z0-9 _\-().]+", "_", (s or "").strip()).strip() or "untitled"


def build_filename(profile_name: str, company: str) -> str:
    """{Profile Name}/Resume ({Company Name}).pdf"""
    return f"{_safe_segment(profile_name)}/Resume ({_safe_segment(company)}).pdf"


async def enqueue_generation(
    *,
    user: UserExtra,
    account: Account,
    jd: str,
    company: str,
    job_url: Optional[str] = None,
    questions: Optional[list[str]] = None,
    prompt_body: Optional[str] = None,
    cover_letter: bool = False,
    source: Literal["generator", "job_applies"] = "generator",
    job_apply_row_id: Optional[PydanticObjectId] = None,
) -> ResumeApplication:
    """Validate and queue one generation. The caller has already checked the user may use `account`."""
    if len((jd or "").encode("utf-8")) > MAX_JD_BYTES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"jobDescription exceeds {MAX_JD_BYTES} bytes")
    if not (jd or "").strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="This job has no description to tailor to.")
    if not (account.styleTemplate or "").strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No HTML template on this profile. Upload one on the Resume Styles tab first.",
        )
    company = (company or "").strip() or "Company"
    profile_name = account.name or "profile"
    job = ResumeApplication(
        userId=user.id,
        accountId=account.id,
        profileName=profile_name,
        companyName=company,
        companyNameLower=company.lower(),
        jobDescription=jd,
        jobUrl=(job_url or "").strip() or None,
        screeningQuestions=[q.strip() for q in questions or [] if q and q.strip()],
        status="queued",
        step="queued",
        pdfFilename=build_filename(profile_name, company),
        generateCoverLetter=bool(cover_letter),
        source=source,
        countsAsBid=False if source == "job_applies" else None,
        promptOverride=(prompt_body or "").strip()[:MAX_PROMPT_CHARS] or None,
        jobApplyRowId=job_apply_row_id,
    )
    await job.insert()
    wake()
    return job


# ---------- worker ----------

_running: set[asyncio.Task] = set()

# Only jobs written by this queue are claimed or recovered: records from before it (no `source`, never claimed)
# that were stuck by old restarts must not be regenerated in bulk.
_QUEUED = {"status": "queued", "source": {"$exists": True}}


def _limit() -> int:
    return max(1, get_settings().RESUME_LLM_CONCURRENCY)


def _coll():
    return ResumeApplication.get_motor_collection()


async def _claim_one() -> Optional[ResumeApplication]:
    """Atomically take the oldest queued job (exactly one worker gets it)."""
    now = datetime.utcnow()
    doc = await _coll().find_one_and_update(
        _QUEUED,
        {"$set": {"status": "in_progress", "step": "generating_resume", "leaseAt": now, "startedAt": now, "updatedAt": now},
         "$inc": {"attempts": 1}},
        sort=[("createdAt", 1)],
        return_document=ReturnDocument.AFTER,
    )
    return await ResumeApplication.get(doc["_id"]) if doc else None


async def _requeue_stale(now: Optional[datetime] = None) -> int:
    """Jobs whose worker died (no heartbeat for STALE_AFTER) go back to the queue, or fail after MAX_ATTEMPTS."""
    now = now or datetime.utcnow()
    stale = {
        "status": "in_progress",
        "attempts": {"$gte": 1},  # claimed by this queue
        "$or": [{"leaseAt": None}, {"leaseAt": {"$lt": now - STALE_AFTER}}],
    }
    await _coll().update_many(
        {**stale, "attempts": {"$gte": MAX_ATTEMPTS}},
        {"$set": {"status": "failed", "errorMessage": "Generation was interrupted repeatedly", "completedAt": now,
                  "updatedAt": now}},
    )
    res = await _coll().update_many(
        {**stale, "attempts": {"$gte": 1, "$lt": MAX_ATTEMPTS}},
        {"$set": {"status": "queued", "step": "queued", "leaseAt": None, "updatedAt": now}},
    )
    if res.modified_count:
        log.info("re-queued %d interrupted resume generation(s)", res.modified_count)
    return res.modified_count


async def _heartbeat(job_id) -> None:
    while True:
        await asyncio.sleep(HEARTBEAT_SECONDS)
        await _coll().update_one({"_id": job_id, "status": "in_progress"}, {"$set": {"leaseAt": datetime.utcnow()}})


async def _run(job: ResumeApplication) -> None:
    hb = asyncio.create_task(_heartbeat(job.id))
    try:
        await RUNNER(job)
    except Exception:  # noqa: BLE001 — the runner records failures on the job; never kill the worker
        log.exception("resume generation %s crashed", job.id)
    finally:
        hb.cancel()
        wake()  # a slot is free


async def run_queue_once() -> int:
    """Start queued jobs until RESUME_LLM_CONCURRENCY are running. Returns how many were started."""
    started = 0
    while len(_running) < _limit():
        job = await _claim_one()
        if job is None:
            break
        task = asyncio.create_task(_run(job))
        _running.add(task)
        task.add_done_callback(_running.discard)
        started += 1
    return started


async def queue_forever() -> None:
    last_sweep = 0.0
    while True:
        try:
            if time.monotonic() - last_sweep >= SWEEP_SECONDS:
                last_sweep = time.monotonic()
                await _requeue_stale()
            _wake.clear()
            await run_queue_once()
        except Exception as exc:  # noqa: BLE001
            log.warning("resume queue loop error: %s", exc)
        try:
            await asyncio.wait_for(_wake.wait(), timeout=POLL_SECONDS)
        except asyncio.TimeoutError:
            pass


def queue_enabled() -> bool:
    return os.environ.get("RESUME_QUEUE_DISABLED") != "1"
