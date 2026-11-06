"""Making a resume job's missing PDF again.

Resume storage moved to a new bucket on 2026-11-05 without copying the old files, so older jobs point at PDFs that are
no longer there (downloads answer 410). `restore_pdf` makes the file again for one completed job, in place:

- a tailored resume is regenerated from the job's stored job description and prompt (resume step only: the cover
  letter, screening answers and match score stay as they were), with the profile's current template;
- a fixed fork (`fixedFrom`, a copy of a profile's uploaded resume) is re-copied from that profile file.

Only the PDF fields (and the resume text, which goes with the new PDF) change. A lease keeps two restores of one job
from running at once.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from app import ai_usage
from app.config import get_settings
from app.models import Account, ResumeApplication, UserExtra
from app.resume_prompts import job_template, resolve_prompt_body

log = logging.getLogger(__name__)

LEASE = timedelta(minutes=10)


class RestoreError(Exception):
    """Why the PDF can't be made again; `status` is the HTTP status to answer with."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def is_missing(exc: Exception) -> bool:
    """A storage error that means "no such object"."""
    response = getattr(exc, "response", None) or {}
    return response.get("Error", {}).get("Code") in ("NoSuchKey", "404", "NotFound")


async def file_exists(key: str) -> bool:
    """Whether the resume bucket holds `key` (a HEAD request, nothing downloaded). Other storage errors propagate."""
    from botocore.exceptions import ClientError

    from app.resume_service import _s3_client

    try:
        await asyncio.to_thread(_s3_client().head_object, Bucket=get_settings().RESUME_STORAGE_BUCKET, Key=key)
        return True
    except ClientError as exc:
        if is_missing(exc):
            return False
        raise


async def _take_lease(job: ResumeApplication) -> bool:
    now = datetime.now(timezone.utc)
    res = await ResumeApplication.get_motor_collection().update_one(
        {"_id": job.id, "$or": [{"pdfRestoreLeaseAt": None}, {"pdfRestoreLeaseAt": {"$lt": now - LEASE}}]},
        {"$set": {"pdfRestoreLeaseAt": now}},
    )
    return res.modified_count == 1


async def _drop_lease(job: ResumeApplication) -> None:
    await ResumeApplication.get_motor_collection().update_one({"_id": job.id}, {"$set": {"pdfRestoreLeaseAt": None}})


async def _copy_fork(job: ResumeApplication) -> dict:
    """A fixed fork: copy the profile's uploaded file back to the job's key."""
    from app.job_apply.fixed_forks import copy_file, fork_key

    account = await Account.get(job.accountId) if job.accountId else None
    resume = next((r for r in (account.resumes if account else []) if str(r.id) == str(job.fixedFrom)), None)
    if resume is None:
        raise RestoreError(422, "The profile resume this was copied from was removed, so it can't be copied again.")
    if not resume.s3Key or not await file_exists(resume.s3Key):
        raise RestoreError(
            422, f"The profile's uploaded file \"{resume.filename}\" is missing too. Upload it again on the profile, "
                 "then try again.",
        )
    key = job.s3Key or fork_key(str(job.accountId), str(job.id), resume.filename)
    await copy_file(resume.s3Key, key)
    return {"s3Key": key, "pdfData": None, "s3Url": None}


async def _regenerate(job: ResumeApplication) -> dict:
    """A tailored resume: the resume step again, with the job's stored JD and prompt, on its model (or the default when
    that model is retired)."""
    from app.resume_service import generate_resume

    jd = (job.tailorJobDescription or "").strip() or (job.jobDescription or "").strip()
    if not jd:
        raise RestoreError(
            422, "This resume's job description is no longer stored (job descriptions are removed after 30 days), so "
                 "it can't be regenerated.",
        )
    account = await Account.get(job.accountId) if job.accountId else None
    if account is None:
        raise RestoreError(422, "This resume's profile no longer exists, so it can't be regenerated.")
    owner = await UserExtra.get(job.userId)
    template_html, page_format = job_template(account, job.promptId)
    with ai_usage.usage_scope(user_id=job.userId, ref_id=job.id):
        result = await generate_resume(
            account, jd, job.companyName, prompt_body=resolve_prompt_body(account, owner, job.promptOverride) or None,
            owner=owner, job_id=str(job.id), model_id=job.resumeModelId, template_html=template_html,
            page_format=page_format,
        )
    if not result.s3_key:
        raise RestoreError(503, "The new resume couldn't be stored. Try again later.")
    fields = {"s3Key": result.s3_key, "pdfData": None, "s3Url": None, "plainText": result.plain_text}
    if result.cost_usd is not None:
        fields["estimatedCostUsd"] = (job.estimatedCostUsd or 0.0) + result.cost_usd
    return fields


async def restore_pdf(job: ResumeApplication) -> dict:
    """Make the job's missing PDF again (see the module docstring). Raises RestoreError when it can't."""
    from app.resume_service import s3_enabled

    if not s3_enabled():
        raise RestoreError(503, "Resume storage isn't configured.")
    if job.status != "completed":
        raise RestoreError(409, "Only finished resumes can be made again.")
    if job.s3Key and await file_exists(job.s3Key):
        raise RestoreError(409, "This resume's file is there: download it again.")
    if not await _take_lease(job):
        raise RestoreError(409, "This resume is already being made again. Try in a minute.")
    try:
        kind = "copied" if job.fixedFrom else "regenerated"
        fields = await (_copy_fork(job) if job.fixedFrom else _regenerate(job))
        fields["pdfRegeneratedAt"] = datetime.now(timezone.utc)
        await ResumeApplication.get_motor_collection().update_one({"_id": job.id}, {"$set": fields})
        log.info("resume job %s: missing PDF %s (%s)", job.id, kind, fields["s3Key"])
        return {"jobId": str(job.id), "kind": kind, "filename": job.pdfFilename or "Resume.pdf"}
    finally:
        await _drop_lease(job)
