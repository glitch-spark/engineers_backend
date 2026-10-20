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
from beanie.odm.utils.encoder import Encoder
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


def _daily_cap() -> int:
    return get_settings().RESUME_DAILY_CAP


async def daily_count(user: UserExtra) -> int:
    """Generations this user queued today (their local day, same as the daily-pace alert), both pages."""
    from app.alerts import local_day_bounds_utc, local_now
    from app.slack_bot import resolve_user_tz

    start, end = local_day_bounds_utc(local_now(resolve_user_tz(user)))
    return await ResumeApplication.find(
        {"userId": user.id, "createdAt": {"$gte": start.replace(tzinfo=None), "$lt": end.replace(tzinfo=None)}}
    ).count()


async def remaining_today(user: UserExtra) -> int:
    return max(0, _daily_cap() - await daily_count(user))


def _check_inputs(
    jd: str, account: Account, template_html: Optional[str] = None, tailor_jd: Optional[str] = None
) -> None:
    """`template_html`: the resume unit's template being used; None = the profile's (default unit's)."""
    if len((jd or "").encode("utf-8")) > MAX_JD_BYTES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"jobDescription exceeds {MAX_JD_BYTES} bytes")
    if tailor_jd and len(tailor_jd.encode("utf-8")) > MAX_JD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=f"tailorJobDescription exceeds {MAX_JD_BYTES} bytes"
        )
    if not (jd or "").strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="This job has no description to tailor to.")
    if not (template_html if template_html is not None else account.styleTemplate or "").strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No HTML template on this profile. Upload one on the Resume Styles tab first.",
        )


async def enqueue_generation(
    *,
    user: UserExtra,
    account: Account,
    jd: str,
    company: str,
    job_url: Optional[str] = None,
    questions: Optional[list[str]] = None,
    prompt_body: Optional[str] = None,
    prompt_id: Optional[str] = None,
    prompt_name: Optional[str] = None,
    template_html: Optional[str] = None,
    template_name: Optional[str] = None,
    cover_letter: bool = False,
    source: Literal["generator", "job_applies"] = "generator",
    job_apply_row_id: Optional[PydanticObjectId] = None,
    resume_model: Optional[str] = None,
    cover_letter_model: Optional[str] = None,
    screening_model: Optional[str] = None,
    cover_letter_hook: Optional[str] = None,
    tailor_jd: Optional[str] = None,
) -> ResumeApplication:
    """Validate and queue one generation. The caller has already checked the user may use `account`.
    `template_html` (checked, not stored: the runner looks the unit up by `prompt_id`) / `template_name`: the resume
    unit's template. `tailor_jd`: the short posting for the resume rewrite; the cover letter and screening keep `jd`."""
    _check_inputs(jd, account, template_html, tailor_jd)
    cap = _daily_cap()
    if await daily_count(user) >= cap:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Daily limit of {cap} tailored resumes reached. It resets at midnight.",
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
        tailorJobDescription=(tailor_jd or "").strip() or None,
        jobUrl=(job_url or "").strip() or None,
        screeningQuestions=[q.strip() for q in questions or [] if q and q.strip()],
        status="queued",
        step="queued",
        pdfFilename=build_filename(profile_name, company),
        generateCoverLetter=bool(cover_letter),
        source=source,
        countsAsBid=False if source == "job_applies" else None,
        promptOverride=(prompt_body or "").strip()[:MAX_PROMPT_CHARS] or None,
        promptId=prompt_id,
        promptName=prompt_name,
        templateName=template_name,
        jobApplyRowId=job_apply_row_id,
        resumeModelId=(resume_model or "").strip() or None,
        coverLetterModelId=(cover_letter_model or "").strip() or None,
        screeningModelId=(screening_model or "").strip() or None,
        coverLetterHook=(cover_letter_hook or "").strip()[:500] or None,
    )
    await job.insert()
    wake()
    return job


async def requeue_failed(
    job_id: PydanticObjectId,
    *,
    account: Account,
    jd: str,
    company: str,
    job_url: Optional[str] = None,
    cover_letter: bool = False,
    resume_model: Optional[str] = None,
    cover_letter_model: Optional[str] = None,
    prompt_body: Optional[str] = None,
    prompt_id: Optional[str] = None,
    prompt_name: Optional[str] = None,
    template_html: Optional[str] = None,
    template_name: Optional[str] = None,
    tailor_jd: Optional[str] = None,
) -> Optional[ResumeApplication]:
    """Re-run a failed Job Applies generation in place with fresh inputs (including the prompt now selected), so a retry keeps one entry in Generated
    Resumes (and adds nothing to the daily count). None when that profile's job is gone or isn't failed."""
    from app.routers.resume import _reset_failed_job_fields  # router imports this module; resolve lazily

    job = await ResumeApplication.find_one({"_id": job_id, "accountId": account.id, "status": "failed"})
    if job is None:
        return None
    _check_inputs(jd, account, template_html, tailor_jd)
    _reset_failed_job_fields(job)
    company = (company or "").strip() or "Company"
    job.profileName = account.name or "profile"
    job.companyName = company
    job.companyNameLower = company.lower()
    job.jobDescription = jd
    job.tailorJobDescription = (tailor_jd or "").strip() or None
    job.jobUrl = (job_url or "").strip() or None
    job.pdfFilename = build_filename(job.profileName, company)
    job.generateCoverLetter = bool(cover_letter)
    job.resumeModelId = (resume_model or "").strip() or None
    job.coverLetterModelId = (cover_letter_model or "").strip() or None
    job.promptOverride = (prompt_body or "").strip()[:MAX_PROMPT_CHARS] or None
    job.promptId, job.promptName, job.templateName = prompt_id, prompt_name, template_name
    job.attempts = 0
    job.leaseAt = None
    job.updatedAt = datetime.utcnow()
    doc = Encoder(to_db=True).encode(job)
    fields = {k: v for k, v in doc.items() if k not in ("_id", "id", "createdAt", "revision_id")}
    res = await _coll().update_one({"_id": job.id, "status": "failed"}, {"$set": fields})
    if res.matched_count == 0:  # a concurrent retry re-queued it first
        return await ResumeApplication.get(job.id)
    wake()
    return job


# ---------- worker ----------

_running: set[asyncio.Task] = set()

# Fields the queue (claim, heartbeat, applied marks) owns; the runner never writes them back.
_QUEUE_OWNED = frozenset({"_id", "id", "createdAt", "leaseAt", "attempts", "countsAsBid", "source", "jobApplyRowId",
                          "promptOverride", "promptId", "promptName", "templateName", "resumeModelId", "coverLetterModelId",
                          "screeningModelId", "coverLetterHook"})


class Superseded(Exception):
    """The job was re-claimed by another worker; this runner must stop writing."""


async def persist_job(job: ResumeApplication) -> None:
    """Save a runner's progress: only the runner's own fields, the lease refreshed, and fenced to this claim
    (`attempts` must still match) so a runner whose job was re-queued can't overwrite the new run."""
    doc = Encoder(to_db=True).encode(job)
    fields = {k: v for k, v in doc.items() if k not in _QUEUE_OWNED}
    now = datetime.utcnow()
    fields.update({"updatedAt": now, "leaseAt": now})
    res = await _coll().update_one({"_id": job.id, "attempts": job.attempts}, {"$set": fields})
    if res.matched_count == 0:
        raise Superseded(str(job.id))


# Only jobs written by this queue are claimed or recovered: records from before it (no `source`, never claimed)
# that were stuck by old restarts must not be regenerated in bulk.
_QUEUED = {"status": "queued", "source": {"$exists": True}}


def _limit() -> int:
    return max(1, get_settings().RESUME_LLM_CONCURRENCY)


def _coll():
    return ResumeApplication.get_motor_collection()


async def _claim_one() -> Optional[ResumeApplication]:
    """Atomically take the oldest queued job (exactly one worker gets it). Generator-page jobs go first, so a
    person waiting on one isn't stuck behind a Job Applies batch."""
    now = datetime.utcnow()
    update = {"$set": {"status": "in_progress", "step": "generating_resume", "leaseAt": now, "startedAt": now,
                       "updatedAt": now},
              "$inc": {"attempts": 1}}
    for match in ({**_QUEUED, "source": "generator"}, _QUEUED):
        doc = await _coll().find_one_and_update(
            match, update, sort=[("createdAt", 1)], return_document=ReturnDocument.AFTER
        )
        if doc:
            return await ResumeApplication.get(doc["_id"])
    return None


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
        try:
            await _coll().update_one({"_id": job_id, "status": "in_progress"}, {"$set": {"leaseAt": datetime.utcnow()}})
        except Exception as exc:  # noqa: BLE001 — one failed beat must not end the lease (it would look stale)
            log.warning("resume job %s heartbeat failed: %s", job_id, exc)


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
    """Opt-in: only the deployed backend sets RESUME_QUEUE_ENABLED=1. Local backends share the production
    database, so a queue that ran by default would take real users' jobs. RESUME_QUEUE_DISABLED=1 still wins."""
    return os.environ.get("RESUME_QUEUE_ENABLED") == "1" and os.environ.get("RESUME_QUEUE_DISABLED") != "1"
