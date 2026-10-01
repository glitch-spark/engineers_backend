"""Persistent queue for resume generation, shared by the Generator page and Job Applies.

`enqueue_generation` records a ResumeApplication as `queued`; the worker (see `queue_forever`) claims queued
jobs with bounded concurrency and runs the existing steps (`app.routers.resume.run_job_doc`).
"""
import asyncio
import logging
from typing import Literal, Optional

from beanie import PydanticObjectId
from fastapi import HTTPException, status

from app.models import Account, ResumeApplication, UserExtra

log = logging.getLogger(__name__)

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
