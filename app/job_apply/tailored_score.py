"""A Job Applies tailored resume scored against its job, with the run's scoring (same terms, weights and IDF).

Scored when the generation finishes (app/routers/resume.py); tailored resumes from before that are scored when
their row is next listed (`backfill`).
"""
import asyncio
import logging
import time
from types import SimpleNamespace
from datetime import date
from typing import Optional

from beanie import PydanticObjectId

from app.job_apply import jd_store
from app.job_apply.resume_parse import parse_resume
from app.job_apply.schema import JobExtraction, TailoredMatch
from app.job_apply.scoring import Idf, build_idf, score_resume
from app.job_apply.sections import substantive_text

log = logging.getLogger(__name__)

MISSING_MAX = 8
BACKFILL_MAX = 25  # per request, so a page of old tailored resumes can't stall the table
IDF_TTL_SECONDS = 600
_idf_cache: dict[str, tuple[float, Idf]] = {}


def tailored_match(ex: JobExtraction, jd: str, text: str, idf: Idf, *, run_date: date) -> TailoredMatch:
    s = score_resume(
        ex, jd, parse_resume(text, run_date), idf,
        run_date=run_date, account_id="", resume_id="", filename="", uploaded_at=None,
    )
    missing = [h.term for h in sorted(s.terms, key=lambda h: -h.weight) if h.credit == 0]
    return TailoredMatch(total=s.total, band=s.band, knockouts=s.knockouts, missing=missing[:MISSING_MAX])


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


async def score_job(job) -> Optional[TailoredMatch]:
    """The finished generation `job` scored, if it's a Job Applies one. Never raises: a score isn't worth a failed job."""
    if getattr(job, "source", None) != "job_applies":
        return None
    try:
        return await score_text(job.jobApplyRowId, job.plainText)
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
