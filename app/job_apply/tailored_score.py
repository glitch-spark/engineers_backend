"""A Job Applies tailored resume scored against its job, with the run's scoring (same terms, weights and IDF).

Scored when the generation finishes (app/routers/resume.py); tailored resumes from before that are scored when
their row is next listed (`backfill`). Resume Generator resumes are scored the same way against their pasted JD
(`score_manual`), when they finish only.
"""
import asyncio
import logging
import time
from types import SimpleNamespace
from datetime import date, datetime
from typing import Optional

from beanie import PydanticObjectId

from app.job_apply import jd_store
from app.job_apply.resume_parse import parse_resume
from app.job_apply.schema import JobExtraction, ResumeScore, TailoredMatch
from app.job_apply.scoring import Idf, build_idf, score_resume
from app.job_apply.sections import substantive_text

log = logging.getLogger(__name__)

MISSING_MAX = 8
BACKFILL_MAX = 25  # per request, so a page of old tailored resumes can't stall the table
IDF_TTL_SECONDS = 600
_idf_cache: dict[str, tuple[float, Idf]] = {}


def match_from_score(s: ResumeScore) -> TailoredMatch:
    """A resume's score with a job, in the shape shown beside tailored resumes (also an uploaded resume's)."""
    missing = [h.term for h in sorted(s.terms, key=lambda h: -h.weight) if h.credit == 0]
    return TailoredMatch(total=s.total, band=s.band, knockouts=s.knockouts, missing=missing[:MISSING_MAX])


def tailored_match(ex: JobExtraction, jd: str, text: str, idf: Idf, *, run_date: date) -> TailoredMatch:
    return match_from_score(score_resume(
        ex, jd, parse_resume(text, run_date), idf,
        run_date=run_date, account_id="", resume_id="", filename="", uploaded_at=None,
    ))


async def _run_idf(run_id: PydanticObjectId) -> Idf:
    """The IDF the run was scored with (every read JD in the run), cached briefly: a bulk tailor finishes many."""
    from app.models.job_apply import JobApplyRow

    key = str(run_id)
    hit = _idf_cache.get(key)
    if hit and time.monotonic() - hit[0] < IDF_TTL_SECONDS:
        return hit[1]
    cursor = JobApplyRow.get_motor_collection().find(
        {"runId": run_id, "extraction": {"$ne": None}}, {"jdText": 1, "jdSource": 1, "url": 1}
    )
    rows = [SimpleNamespace(jdText=d.get("jdText"), jdSource=d.get("jdSource"), url=d.get("url")) async for d in cursor]
    await jd_store.hydrate(rows)
    jds = [r.jdText for r in rows if r.jdText]
    idf = await asyncio.to_thread(lambda: build_idf([substantive_text(jd) or jd for jd in jds]))
    if len(_idf_cache) > 32:
        _idf_cache.clear()
    _idf_cache[key] = (time.monotonic(), idf)
    return idf


async def score_text(row_id: Optional[PydanticObjectId], text: Optional[str]) -> Optional[TailoredMatch]:
    """`text` (a tailored resume) scored against the job on Job Applies row `row_id`; None when it can't be."""
    from app.models.job_apply import JobApplyRow, JobApplyRun

    if not row_id or not (text or "").strip():
        return None
    row = await JobApplyRow.get(row_id)
    await jd_store.hydrate([row])
    if row is None or row.extraction is None or not row.jdText:
        return None
    run = await JobApplyRun.get(row.runId)
    run_date = run.runDate.date() if run else date.today()
    idf = await _run_idf(row.runId)
    return await asyncio.to_thread(tailored_match, row.extraction, row.jdText, text, idf, run_date=run_date)


async def _extraction(jd: str, user) -> JobExtraction:
    """The job read the way Job Applies reads one; cached by JD text, so a JD seen before costs no LLM call."""
    from app.job_apply import extract
    from app.models.job_apply import JobExtractionCache

    key = f"{extract.jd_hash(jd)}:{extract.PROMPT_VERSION}"
    cached = await JobExtractionCache.find_one(JobExtractionCache.key == key)
    if cached:
        return cached.extraction
    ex, source = await extract.extract_job(jd, user=user, run_date=date.today())
    if source == "llm":  # as the Job Applies worker caches it (rules output is cheap to recompute)
        await JobExtractionCache.get_motor_collection().update_one(
            {"key": key},
            {"$set": {"key": key, "extraction": ex.model_dump(mode="json"), "updatedAt": datetime.utcnow()},
             "$setOnInsert": {"createdAt": datetime.utcnow()}},
            upsert=True,
        )
    return ex


async def _user_idf(user_id) -> Idf:
    """No run to weigh terms with: the user's latest Job Applies run's IDF; even weights when they have none."""
    from app.models.job_apply import JobApplyRun

    run = await JobApplyRun.find(JobApplyRun.userId == user_id).sort("-createdAt").first_or_none() if user_id else None
    return await _run_idf(run.id) if run else build_idf([])


async def score_manual(jd: Optional[str], text: Optional[str], user) -> Optional[TailoredMatch]:
    """`text` (a resume from the Resume Generator) scored against the JD pasted with it; None when it can't be."""
    if not (jd or "").strip() or not (text or "").strip():
        return None
    ex = await _extraction(jd, user)
    if not ex.isJobPosting or not ex.terms:
        return None
    idf = await _user_idf(getattr(user, "id", None))
    return await asyncio.to_thread(tailored_match, ex, jd, text, idf, run_date=date.today())


async def score_job(job, user=None) -> Optional[TailoredMatch]:
    """The finished generation `job` scored against its job: the Job Applies row's, or the JD pasted on the Resume
    Generator (read for `user`). Never raises: a score isn't worth a failed job."""
    try:
        if getattr(job, "source", None) == "job_applies":
            return await score_text(job.jobApplyRowId, job.plainText)
        return await score_manual(job.jobDescription, job.plainText, user)
    except Exception as exc:  # noqa: BLE001
        log.warning("scoring tailored resume %s failed: %s", getattr(job, "id", "?"), exc)
        return None


async def backfill(docs: dict[str, dict], ids: Optional[list] = None) -> None:
    """Score completed tailored resumes that have no `matchScore` yet (older than scoring), at most BACKFILL_MAX;
    `docs` (id → generation doc, as the router loads them) gets the scores. `ids` limits it to those generations.
    One that can't be scored is stored as null so it isn't tried again."""
    from app.models.resume_application import ResumeApplication

    wanted = {str(i) for i in ids} if ids is not None else None
    todo = [
        d["_id"] for k, d in docs.items()
        if d.get("status") == "completed" and "matchScore" not in d and (wanted is None or k in wanted)
    ][:BACKFILL_MAX]
    if not todo:
        return
    coll = ResumeApplication.get_motor_collection()
    async for d in coll.find({"_id": {"$in": todo}}, {"plainText": 1, "jobApplyRowId": 1}):
        try:
            match = await score_text(d.get("jobApplyRowId"), d.get("plainText"))
        except Exception as exc:  # noqa: BLE001
            log.warning("scoring tailored resume %s failed: %s", d["_id"], exc)
            continue
        value = match.model_dump() if match else None
        await coll.update_one({"_id": d["_id"], "matchScore": {"$exists": False}}, {"$set": {"matchScore": value}})
        docs[str(d["_id"])]["matchScore"] = value
