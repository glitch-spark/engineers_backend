"""Background worker for a Job Applies run (spec §4.3).

Stages: fetch (URL-only rows) → extract (LLM per job, cached) → IDF over the run → score → done. Every row
is saved as soon as it changes stage, and each stage only picks rows that still need it, so a run
interrupted by a redeploy resumes where it stopped. A heartbeat marks the run as owned; the periodic sweep
restarts runs whose heartbeat went stale.
"""
import asyncio
import logging
import os
from datetime import date, datetime, timedelta
from typing import Literal, Optional

import httpx
from beanie import PydanticObjectId
from beanie.odm.utils.encoder import Encoder

from app.job_apply import extract as extract_mod
from app.job_apply import fetch as fetch_mod
from app.job_apply.gates import blocked, failing_reasons, job_gates
from app.job_apply.screening import NO_LOCATION, WORTH, bucket, group_key, markets_for
from app.job_apply.pipeline import ProfileInfo, ResumeInput, score_row
from app.job_apply.resume_parse import parse_resume
from app.job_apply.schema import GateResult, JobExtraction
from app.job_apply.scoring import build_idf
from app.job_apply.sections import substantive_text
from app.models.account import Account
from app.models.job_apply import CACHE_DAYS, JobApplyRow, JobApplyRun, JobExtractionCache, JobPostingCache, ProfileGates
from app.models.user_extra import UserExtra

log = logging.getLogger(__name__)

# Module attributes so tests can substitute fakes.
FETCH = fetch_mod.fetch_job
EXTRACT = extract_mod.extract_job
BROWSER = fetch_mod.browser_html

FETCH_CONCURRENCY = 6
LLM_CONCURRENCY = 4
HEARTBEAT_SECONDS = 30
STALE_AFTER = timedelta(minutes=2)
SWEEP_SECONDS = 120
CHECK_EVERY = 25
POSTING_TTL = timedelta(days=CACHE_DAYS)
# A cached description is reused only this long, so a job closed since then is noticed (the LLM reading is
# cached by description, so refetching an unchanged page costs no LLM call).
POSTING_REUSE = timedelta(hours=24)

Stage = Literal["fetch", "extract", "score"]

_tasks: set[asyncio.Task] = set()
_active: set[str] = set()


class _Cancelled(Exception):
    pass


# ---------- pure helpers ----------


def needs_stage(status: str, *, has_jd: bool, scored: bool, stage: Stage) -> bool:
    if stage == "fetch":
        return status == "pending" and not has_jd
    if stage == "extract":
        return status in ("pending", "fetched") and has_jd
    return status in ("extracted", "excluded") and not scored


def posting_reusable(fetched_at: datetime, now: datetime) -> bool:
    return fetched_at > now - POSTING_REUSE


def assigned_profiles(assignments: dict[str, list], key: Optional[str]) -> set[str]:
    """Profiles to score a job against: those picked for each candidate market its location group is in (or for
    "none" when it states no location), plus those picked for every job ("*")."""
    keys = markets_for(key) or ([NO_LOCATION] if (key or NO_LOCATION) == NO_LOCATION else [])
    picked = {str(x) for k in keys for x in assignments.get(k, [])}
    return picked | {str(x) for x in assignments.get("*", [])}


def _screen(row: JobApplyRow) -> None:
    row.screen = bucket(
        status=row.status, status_reason=row.statusReason, gates=row.gates or [],
        group_key=row.groupKey, force_include=row.forceInclude,
        is_job=row.extraction.isJobPosting if row.extraction is not None else True,
        verified=row.jdStructured is not False,
    )


def posted_for_row(fetched: Optional[date], extracted: Optional[date]) -> Optional[date]:
    return fetched or extracted


# ---------- persistence helpers ----------


async def _inc(run_id: PydanticObjectId, **fields: int) -> None:
    await JobApplyRun.get_motor_collection().update_one(
        {"_id": run_id}, {"$inc": {f"counts.{k}": v for k, v in fields.items()}}
    )


async def _set_run(run_id: PydanticObjectId, **fields) -> None:
    fields["updatedAt"] = datetime.utcnow()
    await JobApplyRun.get_motor_collection().update_one({"_id": run_id}, {"$set": fields})


async def _status(run_id: PydanticObjectId) -> Optional[str]:
    doc = await JobApplyRun.get_motor_collection().find_one({"_id": run_id}, {"status": 1})
    return doc.get("status") if doc else None


_FETCH_FIELDS = (
    "jdText", "jdSource", "jdStructured", "fetchedPostedDate", "title", "company", "status", "statusReason", "screen",
)
_EXTRACT_FIELDS = (
    "extraction", "extractionSource", "gates", "postedDate", "title", "company", "status", "statusReason", "screen", "groupKey",
)
_SCORE_FIELDS = ("scores", "profileGates", "bestByProfile", "topScore", "tailorOnly", "scoredAt", "status")


async def _save_fields(row: JobApplyRow, fields: tuple[str, ...]) -> None:
    """$set only what a stage changed. Never `applied` (the user may tick it mid-run) and never an upsert
    (a run deleted after Cancel must stay deleted)."""
    doc = Encoder(to_db=True).encode({f: getattr(row, f) for f in fields})
    doc["updatedAt"] = datetime.utcnow()
    await JobApplyRow.get_motor_collection().update_one({"_id": row.id}, {"$set": doc})


async def _gather_cancellable(coros) -> None:
    """gather(), except that when one task fails (e.g. _Cancelled) the others are cancelled, not left running."""
    tasks = [asyncio.create_task(c) for c in coros]
    try:
        await asyncio.gather(*tasks)
    except BaseException:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


async def _ensure_running(run_id: PydanticObjectId) -> None:
    if await _status(run_id) == "cancelled":
        raise _Cancelled()


class _Checker:
    """Re-reads the run status every CHECK_EVERY processed rows; raises _Cancelled when cancelled."""

    def __init__(self, run_id: PydanticObjectId):
        self.run_id = run_id
        self.n = 0
        self.cancelled = False

    async def tick(self) -> None:
        # Sticky: once any task sees the cancel, every other task stops at its next tick.
        if self.cancelled:
            raise _Cancelled()
        self.n += 1
        if self.n % CHECK_EVERY == 0 and await _status(self.run_id) == "cancelled":
            self.cancelled = True
            raise _Cancelled()


async def _rows(run_id: PydanticObjectId) -> list[JobApplyRow]:
    return await JobApplyRow.find(JobApplyRow.runId == run_id).sort("+rowIndex").to_list()


async def _cache_description(url: str, text: str) -> None:
    """Remember a description that came in a sheet, so a later link-only row for the URL needs no fetch."""
    now = datetime.utcnow()
    await JobPostingCache.get_motor_collection().update_one(
        {"url": url},
        {"$set": {"url": url, "text": text, "source": "sheet", "postedDate": None, "title": "", "company": "",
                  "fetchedAt": now, "updatedAt": now, "fetcher": fetch_mod.FETCHER_VERSION, "structured": True},
         "$setOnInsert": {"createdAt": now}},
        upsert=True,
    )


# ---------- stages ----------


async def _fetch_one(row: JobApplyRow, client: httpx.AsyncClient) -> None:
    cached = await JobPostingCache.find_one(JobPostingCache.url == row.url)
    if cached and cached.fetcher != fetch_mod.FETCHER_VERSION:
        cached = None  # read before the current rules (e.g. a closed job's 404 page): fetch again
    res = None
    if not (cached and posting_reusable(cached.fetchedAt, datetime.utcnow())):
        try:
            res = await FETCH(row.url, client=client, browser=BROWSER)
        except fetch_mod.JobClosed:
            raise
        except Exception:
            # A blocked or timed-out refetch keeps the description read earlier (within the cache's life).
            if not (cached and cached.fetchedAt > datetime.utcnow() - POSTING_TTL):
                raise
    if res is None:
        text, source, posted, title, company = cached.text, cached.source, cached.postedDate, cached.title, cached.company
        structured = cached.structured
    else:
        text, source, posted, title, company = res.text, res.source, res.postedDate, res.title, res.company
        structured = res.structured
        coll = JobPostingCache.get_motor_collection()
        await coll.update_one(
            {"url": row.url},
            {"$set": {
                "url": row.url, "text": text, "source": source,
                "postedDate": datetime.combine(posted, datetime.min.time()) if posted else None,
                "title": title, "company": company, "fetchedAt": datetime.utcnow(), "updatedAt": datetime.utcnow(),
                "fetcher": fetch_mod.FETCHER_VERSION, "structured": structured,
            }, "$setOnInsert": {"createdAt": datetime.utcnow()}},
            upsert=True,
        )
    row.jdText, row.jdSource, row.fetchedPostedDate, row.jdStructured = text, source, posted, structured
    row.title = row.title or title
    row.company = row.company or company
    row.status = "fetched"


async def _fetch_stage(run: JobApplyRun, rows: list[JobApplyRow], check: _Checker) -> None:
    todo = [r for r in rows if needs_stage(r.status, has_jd=bool(r.jdText), scored=bool(r.scoredAt), stage="fetch")]
    if not todo:
        return
    sem = asyncio.Semaphore(FETCH_CONCURRENCY)
    async with httpx.AsyncClient() as client:

        async def one(row: JobApplyRow) -> None:
            async with sem:
                await check.tick()
                try:
                    await _fetch_one(row, client)
                    await _save_fields(row, _FETCH_FIELDS)
                    await _inc(run.id, fetched=1)
                except _Cancelled:
                    raise
                except Exception as exc:  # noqa: BLE001 — a row failure never fails the run
                    row.status, row.statusReason = "fetch_failed", str(exc)[:300] or exc.__class__.__name__
                    _screen(row)
                    await _save_fields(row, _FETCH_FIELDS)
                    await _inc(run.id, failed=1)

        await _gather_cancellable(one(r) for r in todo)


async def _extract_one(row: JobApplyRow, run: JobApplyRun, user: Optional[UserExtra]) -> None:
    run_date = run.runDate.date()
    key = f"{extract_mod.jd_hash(row.jdText)}:{extract_mod.PROMPT_VERSION}"
    cached = await JobExtractionCache.find_one(JobExtractionCache.key == key)
    if cached:
        ex, source = cached.extraction, "cache"
    else:
        ex, source = await EXTRACT(row.jdText, user=user, run_date=run_date)
        if source == "llm":  # rules output is cheap to recompute and shouldn't block a later LLM retry
            await JobExtractionCache.get_motor_collection().update_one(
                {"key": key},
                {"$set": {"key": key, "extraction": ex.model_dump(mode="json"), "updatedAt": datetime.utcnow()},
                 "$setOnInsert": {"createdAt": datetime.utcnow()}},
                upsert=True,
            )
    posted = posted_for_row(row.fetchedPostedDate, ex.postedDate)
    gates = job_gates(ex, run_date=run_date, max_age_days=run.maxAgeDays, posted=posted)
    row.extraction, row.extractionSource, row.gates, row.postedDate = ex, source, gates, posted
    row.groupKey = group_key(ex.allowedLocations)
    row.title = row.title or ex.title
    row.company = row.company or ex.company
    if blocked(gates):
        row.status, row.statusReason = "excluded", failing_reasons(gates)
    else:
        row.status, row.statusReason = "extracted", None
    _screen(row)


async def _extract_stage(run: JobApplyRun, rows: list[JobApplyRow], user: Optional[UserExtra], check: _Checker) -> None:
    todo = [r for r in rows if needs_stage(r.status, has_jd=bool(r.jdText), scored=bool(r.scoredAt), stage="extract")]
    sem = asyncio.Semaphore(LLM_CONCURRENCY)

    async def one(row: JobApplyRow) -> None:
        async with sem:
            await check.tick()
            try:
                if row.jdSource == "sheet" and row.url:
                    await _cache_description(row.url, row.jdText)
                await _extract_one(row, run, user)
                await _save_fields(row, _EXTRACT_FIELDS)
                await _inc(run.id, extracted=1, excluded=1 if row.status == "excluded" else 0)
            except _Cancelled:
                raise
            except Exception as exc:  # noqa: BLE001
                log.exception("job extraction failed for row %s", row.id)
                row.status, row.statusReason = "llm_failed", str(exc)[:300] or exc.__class__.__name__
                _screen(row)
                await _save_fields(row, _EXTRACT_FIELDS)
                await _inc(run.id, failed=1)

    await _gather_cancellable(one(r) for r in todo)


async def _load_resumes(run: JobApplyRun) -> tuple[list[ResumeInput], dict[str, ProfileInfo], list[str]]:
    run_date = run.runDate.date()
    resumes: list[ResumeInput] = []
    profiles: dict[str, ProfileInfo] = {}
    notes: list[str] = []
    for sel in run.selection:
        acc = await Account.get(sel.accountId)
        if acc is None or acc.archived:
            notes.append(f"Profile {sel.accountId} is no longer available; its resumes were skipped.")
            continue
        wanted = {str(x) for x in sel.resumeIds}
        acc_id = str(acc.id)
        profiles[acc_id] = ProfileInfo(account_id=acc_id, name=acc.name, country=acc.country, region=acc.region)
        before = len(resumes)
        for r in acc.resumes:
            if wanted and str(r.id) not in wanted:
                continue
            resumes.append(ResumeInput(
                account_id=acc_id, resume_id=str(r.id), filename=r.filename, uploaded_at=r.uploadedAt,
                parsed=parse_resume(r.markdown, run_date),
            ))
        if len(resumes) == before and (acc.styleTemplate or "").strip():
            profiles[acc_id].tailor_only = True
    return resumes, profiles, notes


async def _score_stage(run: JobApplyRun, rows: list[JobApplyRow], check: _Checker) -> None:
    # Only jobs worth applying to are scored, and only against the profiles picked for their location group.
    todo = [
        r for r in rows
        if needs_stage(r.status, has_jd=bool(r.jdText), scored=bool(r.scoredAt), stage="score") and r.screen in WORTH
    ]
    if not todo:
        return
    resumes, profiles, notes = await _load_resumes(run)
    if notes:
        await JobApplyRun.get_motor_collection().update_one({"_id": run.id}, {"$addToSet": {"notes": {"$each": notes}}})
    jds = [r.jdText for r in rows if r.extraction and r.jdText]
    idf = await asyncio.to_thread(lambda: build_idf([substantive_text(jd) or jd for jd in jds]))
    run_date = run.runDate.date()
    for row in todo:
        await check.tick()
        allowed = assigned_profiles(run.assignments, row.groupKey)
        if not allowed:
            row.status = "unassigned"
            await _save_fields(row, ("status",))
            continue
        ex: JobExtraction = row.extraction
        # CPU-bound: run off the event loop so the API stays responsive during big runs.
        res = await asyncio.to_thread(
            score_row, ex, row.jdText, job_blocked=False,
            resumes=[r for r in resumes if r.account_id in allowed],
            profiles={k: v for k, v in profiles.items() if k in allowed}, idf=idf, run_date=run_date,
        )
        row.scores = res.scores
        row.profileGates = [ProfileGates(**pg) for pg in res.profile_gates]
        row.bestByProfile = res.best_by_profile
        row.topScore = res.top_score
        row.tailorOnly = res.tailor_only
        row.scoredAt = datetime.utcnow()
        row.status = "scored"  # also a job brought back with "Include anyway"
        await _save_fields(row, _SCORE_FIELDS)
        await _inc(run.id, scored=1)


# ---------- run lifecycle ----------


async def _heartbeat(run_id: PydanticObjectId) -> None:
    while True:
        await _set_run(run_id, heartbeatAt=datetime.utcnow())
        await asyncio.sleep(HEARTBEAT_SECONDS)


async def run_worker(run_id: PydanticObjectId) -> None:
    key = str(run_id)
    if key in _active:
        return
    _active.add(key)
    hb: Optional[asyncio.Task] = None
    try:
        run = await JobApplyRun.get(run_id)
        if run is None or run.status in ("done", "cancelled"):
            return
        await _set_run(run_id, status="running", heartbeatAt=datetime.utcnow(), error=None)
        hb = asyncio.create_task(_heartbeat(run_id))
        user = await UserExtra.get(run.userId)
        check = _Checker(run_id)
        rows = await _rows(run_id)
        # Fetch and read whatever still needs it (both stages skip finished rows), in either phase: a retry after
        # scoring started, or a run resumed after a restart, picks its pending rows up here.
        await _ensure_running(run_id)
        await _fetch_stage(run, rows, check)
        await _ensure_running(run_id)
        await _extract_stage(run, rows, user, check)
        await _ensure_running(run_id)
        if run.phase == "screen":
            if not run.autoStart:  # wait for the user to pick profiles per location group (POST /start)
                await _set_run(run_id, status="screened", screenedAt=datetime.utcnow())
                return
            run.phase = "score"
            await _set_run(run_id, phase="score", screenedAt=datetime.utcnow())
        await _ensure_running(run_id)
        await _score_stage(run, rows, check)
        if await _status(run_id) != "cancelled":
            await _set_run(run_id, status="done", finishedAt=datetime.utcnow())
    except _Cancelled:
        log.info("job-apply run %s cancelled", run_id)
    except Exception as exc:  # noqa: BLE001 — record and surface; retry resumes the run
        log.exception("job-apply run %s failed", run_id)
        await _set_run(run_id, status="failed", error=str(exc)[:500] or exc.__class__.__name__)
    finally:
        if hb:
            hb.cancel()
        _active.discard(key)


def start_run(run_id: PydanticObjectId) -> None:
    task = asyncio.create_task(run_worker(run_id))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def resume_stale_runs() -> int:
    """Restart queued/running runs whose worker died (no heartbeat for STALE_AFTER)."""
    cutoff = datetime.utcnow() - STALE_AFTER
    docs = await JobApplyRun.get_motor_collection().find(
        {"status": {"$in": ["queued", "running"]},
         "$or": [{"heartbeatAt": None}, {"heartbeatAt": {"$lt": cutoff}}]},
        {"_id": 1},
    ).to_list(length=None)
    started = 0
    for d in docs:
        if str(d["_id"]) not in _active:
            start_run(d["_id"])
            started += 1
    if started:
        log.info("resumed %d stale job-apply runs", started)
    return started


async def sweep_forever() -> None:
    """Started from the app lifespan unless JOB_APPLY_WORKER_DISABLED=1."""
    while True:
        try:
            await resume_stale_runs()
        except Exception as exc:  # noqa: BLE001
            log.warning("job-apply sweep failed: %s", exc)
        await asyncio.sleep(SWEEP_SECONDS)


def worker_enabled() -> bool:
    return os.environ.get("JOB_APPLY_WORKER_DISABLED") != "1"
