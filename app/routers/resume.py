import asyncio
import base64
import json
import logging
import re
import time
from datetime import datetime, timezone
from typing import Annotated, Any, List, Optional

from beanie import PydanticObjectId
from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field

from app.config import get_settings
from app.deps import get_current_user
from app.models import Account, ResumeApplication, UserExtra
from app.models.resume_application import ScreeningPair as ScreeningPairModel
from app.resume_service import (
    default_prompt_body,
    default_screening_prompt_body,
    generate_resume,
    generate_presigned_get,
    s3_enabled,
)
from app.routers._util import doc_to_dict, to_object_id

router = APIRouter(prefix="/resume", tags=["resume"])
log = logging.getLogger(__name__)

MAX_JD_BYTES = 50_000

_SAFE_FILENAME_SEG = re.compile(r"[^A-Za-z0-9 _\-().]+")


def _safe_filename_segment(s: str) -> str:
    return _SAFE_FILENAME_SEG.sub("_", (s or "").strip()).strip() or "untitled"


def _build_filename(profile_name: str, company: str) -> str:
    """{Profile Name}/Resume ({Company Name}).pdf — slash kept as a path
    segment so S3 archives nest cleanly per profile."""
    return f"{_safe_filename_segment(profile_name)}/Resume ({_safe_filename_segment(company)}).pdf"


async def _load_owned_account(account_id: str, user: UserExtra) -> Account:
    account = await Account.get(to_object_id(account_id))
    is_admin = user.role == "admin"
    if account is None or (not is_admin and str(account.createdBy) != str(user.id)):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    return account


# ---------- Job lifecycle (async generation) ---------------------------------


class GenerateBody(BaseModel):
    accountId: str
    company: str = Field(min_length=1)
    jobDescription: str = Field(min_length=1)
    jobUrl: Optional[str] = None
    questions: List[str] = []
    # One-off resume content-prompt override for this generation only. Wins
    # over account-level and user-level saved prompts. Empty → fall through.
    promptBody: Optional[str] = None


@router.post("/templates/{account_id}/annotate")
async def annotate_account_template(
    account_id: str,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    """Run the one-time LLM conversion: plain styleTemplate → marker-annotated
    template. Persists styleTemplateAnnotated and returns it + validation."""
    from app.html_sanitize import sanitize_template_html
    from app.template_annotate import annotate_template
    from app.template_fill import validate_annotated

    account = await _load_owned_account(account_id, user)
    plain = (account.styleTemplate or "").strip()
    if not plain:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Upload an HTML template first.",
        )

    annotated = await annotate_template(sanitize_template_html(plain))
    annotated = sanitize_template_html(annotated)
    missing_req, missing_rec = validate_annotated(annotated)

    account.styleTemplateAnnotated = annotated
    await account.save()

    return {
        "annotated": annotated,
        "missingRequired": missing_req,
        "missingRecommended": missing_rec,
        "valid": not missing_req,
    }


@router.get("/default-prompt")
async def get_default_prompt(
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    """Built-in resume content-contract prompt. Frontend 'Load default' button
    seeds the custom-prompt textareas from this."""
    return {"promptBody": default_prompt_body()}


@router.get("/default-screening-prompt")
async def get_default_screening_prompt(
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    """Built-in screening-answer prompt for the 'Load default' button."""
    return {"promptBody": default_screening_prompt_body()}


@router.post("/generate", status_code=status.HTTP_202_ACCEPTED)
async def generate(
    body: GenerateBody,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    """Enqueue a resume-generation job and return immediately.

    The actual OpenAI + PDF + (optional) screening work happens in a
    background asyncio task. Frontend polls GET /resume/jobs to follow
    progress.
    """
    if len(body.jobDescription.encode("utf-8")) > MAX_JD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"jobDescription exceeds {MAX_JD_BYTES} bytes",
        )

    account = await _load_owned_account(body.accountId, user)
    if not (account.experience or "").strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Account is missing the 'experience' field — fill it on the Accounts page first.",
        )
    from app.template_fill import validate_annotated
    annotated = (account.styleTemplateAnnotated or "").strip()
    if not annotated:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No annotated template. On Resume Styles, upload a template and run 'Convert to template'.",
        )
    missing_req, _ = validate_annotated(annotated)
    if missing_req:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Annotated template missing required markers: " + ", ".join(missing_req)
            + ". Re-run 'Convert to template' on Resume Styles.",
        )

    company = body.company.strip()
    questions = [q.strip() for q in body.questions if q and q.strip()]
    profile_name = account.name or "profile"

    job = ResumeApplication(
        userId=user.id,
        accountId=account.id,
        profileName=profile_name,
        companyName=company,
        companyNameLower=company.lower(),
        jobDescription=body.jobDescription,
        jobUrl=(body.jobUrl or "").strip() or None,
        screeningQuestions=questions,
        status="queued",
        step="queued",
        pdfFilename=_build_filename(profile_name, company),
    )
    await job.insert()

    prompt_body_override = (body.promptBody or "").strip()[:30000] or None

    asyncio.create_task(
        _run_job(
            job_id=job.id,
            account_id=account.id,
            user_id=user.id,
            jd=body.jobDescription,
            company=company,
            questions=questions,
            prompt_body_override=prompt_body_override,
        )
    )

    return {"jobId": str(job.id), "status": job.status}


async def _run_job(
    job_id: PydanticObjectId,
    account_id: PydanticObjectId,
    user_id: PydanticObjectId,
    jd: str,
    company: str,
    questions: List[str],
    prompt_body_override: Optional[str] = None,
) -> None:
    """Background orchestrator: resume → optional screening, persisting state."""
    started = time.monotonic()
    job = await ResumeApplication.get(job_id)
    if job is None:
        return
    account = await Account.get(account_id)
    if account is None:
        await _mark_failed(job, "account no longer exists", started)
        return
    owner = await UserExtra.get(user_id)

    # Resolve resume content-prompt: per-generation > account > user > default.
    resolved_prompt_body = (prompt_body_override or "").strip()
    if not resolved_prompt_body:
        resolved_prompt_body = (account.resumePromptBody or "").strip()
    if not resolved_prompt_body:
        resolved_prompt_body = (owner.resumePromptBody or "").strip() if owner else ""
    # Empty → generate_resume falls back to the built-in default.

    # Resolve screening prompt: account > user > built-in default.
    resolved_screening = (account.screeningPrompt or "").strip()
    if not resolved_screening:
        resolved_screening = (owner.screeningPromptBody or "").strip() if owner else ""
    if not resolved_screening:
        resolved_screening = default_screening_prompt_body()

    try:
        # ---- step 1: generate resume ----
        job.status = "in_progress"
        job.step = "generating_resume"
        job.startedAt = datetime.now(timezone.utc)
        await job.save()

        result = await generate_resume(
            account, jd, company, prompt_body=resolved_prompt_body or None
        )

        # ---- step 2: persist PDF reference ----
        # Prefer S3: bucket pointer only, no inline bytes. Fall back to base64
        # in Mongo when S3 not configured (dev convenience).
        job.step = "uploading"
        job.s3Key = result.s3_key
        if result.s3_key:
            job.pdfData = None  # bytes live in S3 now
            job.s3Url = None    # legacy public URL — downloads use presigned URLs
        else:
            job.pdfData = base64.b64encode(result.pdf_bytes).decode("ascii")
        await job.save()
        log.info(
            "job %s pdf persisted: %d bytes (s3_key=%s)",
            job.id, len(result.pdf_bytes), result.s3_key or "<inline>",
        )

        # ---- step 3: screening (optional) ----
        if questions:
            job.step = "generating_answers"
            await job.save()

            pairs = await _run_screening(
                account=account,
                jd=jd,
                questions=questions,
                screening_prompt=resolved_screening,
            )
            # Coerce dict -> ScreeningPair so Beanie/Pydantic doesn't mis-validate on save.
            job.screeningPairs = [ScreeningPairModel(**p) for p in pairs]

        # ---- done ----
        job.status = "completed"
        job.step = "done"
        job.completedAt = datetime.now(timezone.utc)
        job.executionMs = int((time.monotonic() - started) * 1000)
        await job.save()

    except Exception as exc:  # noqa: BLE001 — capture *any* failure on the job
        log.exception("resume job %s failed", job_id)
        await _mark_failed(job, str(exc), started)


async def _mark_failed(job: ResumeApplication, message: str, started: float) -> None:
    job.status = "failed"
    job.errorMessage = message[:1000]
    job.completedAt = datetime.now(timezone.utc)
    job.executionMs = int((time.monotonic() - started) * 1000)
    try:
        await job.save()
    except Exception:  # noqa: BLE001
        log.exception("could not persist failure for job %s", job.id)


# ---------- Screening (extracted helper, reused by job + standalone endpoint) -


async def _run_screening(
    account: Account,
    jd: str,
    questions: List[str],
    screening_prompt: str,
) -> List[dict]:
    """`screening_prompt` is the resolved answer-prompt (account > user >
    built-in default), resolved by the caller."""
    from openai import AsyncOpenAI

    settings = get_settings()
    if not settings.OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY not configured")

    # Pull the first show-true contact item with a display value as a rough
    # "email" proxy for the screening prompt — generic contact items have no
    # type, so the LLM gets whichever the user prioritized first.
    primary_contact = next(
        (ci.display for ci in (account.contactItems or []) if ci.show and (ci.display or "").strip()),
        "",
    )
    parts: List[str] = [
        "You answer job-application screening questions on behalf of a candidate.",
        f"Candidate name: {account.name}.",
        f"Candidate primary contact: {primary_contact}." if primary_contact else "",
        f"Candidate education: {account.education}." if account.education else "",
    ]
    if (account.experience or "").strip():
        parts.append(f"Candidate experience:\n{account.experience}")

    if (jd or "").strip():
        parts.append(f"Job description:\n{jd}")

    indexed = "\n".join(f"{i + 1}. {q}" for i, q in enumerate(questions))
    parts.append((screening_prompt or default_screening_prompt_body()).strip())
    parts.append(f"Questions:\n{indexed}")
    parts.append(
        f"Return a JSON object of the exact form: {{\"answers\": [\"...\", \"...\"]}} "
        f"with exactly {len(questions)} entries in the same order as the numbered questions. "
        "Do not include any text outside the JSON."
    )

    client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
    response = await client.responses.create(
        model=settings.RESUME_OPENAI_MODEL,
        input="\n\n".join(p for p in parts if p),
    )
    raw = response.output_text or ""

    answers: List[str] = []
    try:
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.MULTILINE)
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict) and isinstance(parsed.get("answers"), list):
            answers = [str(a) for a in parsed["answers"]]
    except (json.JSONDecodeError, ValueError):
        pass
    if not answers:
        chunks = re.split(r"^\s*\d+\.\s*", raw, flags=re.MULTILINE)
        answers = [c.strip() for c in chunks if c.strip()]
    while len(answers) < len(questions):
        answers.append("")
    answers = answers[: len(questions)]

    return [{"question": q, "answer": a} for q, a in zip(questions, answers)]


class ScreeningBody(BaseModel):
    accountId: str
    jobDescription: Optional[str] = ""
    questions: List[str]


@router.post("/screening-answers")
async def screening_answers(
    body: ScreeningBody,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    """Standalone screening — answers without a full resume regeneration.
    Screening prompt resolves account > user > built-in default."""
    questions = [q.strip() for q in body.questions if q and q.strip()]
    if not questions:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="At least one non-empty question is required.",
        )

    account = await _load_owned_account(body.accountId, user)
    if not (account.experience or "").strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Account is missing the 'experience' field — fill it on the Accounts page first.",
        )

    screening = (account.screeningPrompt or "").strip()
    if not screening:
        screening = (user.screeningPromptBody or "").strip()
    if not screening:
        screening = default_screening_prompt_body()

    pairs = await _run_screening(
        account=account,
        jd=body.jobDescription or "",
        questions=questions,
        screening_prompt=screening,
    )
    return {"pairs": pairs}


# ---------- Job listing / polling / download ---------------------------------


def _job_to_dict(job: ResumeApplication) -> dict:
    """Slim projection — never ships pdfData (multi-MB) over the list API.
    Clients call /resume/jobs/{id}/download for the actual bytes."""
    d: dict[str, Any] = {
        "_id": str(job.id),
        "userId": str(job.userId),
        "accountId": str(job.accountId) if job.accountId else None,
        "profileName": job.profileName,
        "companyName": job.companyName,
        "jobUrl": job.jobUrl,
        "status": job.status,
        "step": job.step,
        "pdfFilename": job.pdfFilename,
        "s3Key": job.s3Key,
        "errorMessage": job.errorMessage,
        "executionMs": job.executionMs,
        "screeningQuestions": job.screeningQuestions,
        "screeningPairs": [p.model_dump() for p in job.screeningPairs],
        "hasPdf": bool(job.s3Key) or bool(job.pdfData),
        "createdAt": job.createdAt.isoformat() if job.createdAt else None,
        "startedAt": job.startedAt.isoformat() if job.startedAt else None,
        "completedAt": job.completedAt.isoformat() if job.completedAt else None,
    }
    return d


@router.get("/jobs")
async def list_jobs(
    user: Annotated[UserExtra, Depends(get_current_user)],
    accountId: Optional[str] = None,
    company: Optional[str] = None,
    page: int = 1,
    limit: int = 20,
) -> dict:
    # Strictly own jobs — admin sees only own profiles' resumes too.
    query: dict = {"userId": user.id}
    if accountId:
        query["accountId"] = to_object_id(accountId)
    if company and company.strip():
        # Case-insensitive substring match on company name.
        query["companyNameLower"] = {"$regex": re.escape(company.strip().lower())}
    page = max(1, page)
    limit = max(1, min(100, limit))
    total = await ResumeApplication.find(query).count()
    rows = (
        await ResumeApplication.find(query)
        .sort("-createdAt")
        .skip((page - 1) * limit)
        .limit(limit)
        .to_list()
    )
    total_pages = (total + limit - 1) // limit if limit else 0
    return {
        "jobs": [_job_to_dict(r) for r in rows],
        "pagination": {
            "page": page,
            "limit": limit,
            "total": total,
            "totalPages": total_pages,
            "hasNext": page < total_pages,
            "hasPrev": page > 1,
        },
    }


@router.get("/jobs/{job_id}")
async def get_job(
    job_id: str,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    job = await ResumeApplication.get(to_object_id(job_id))
    if job is None or (user.role != "admin" and str(job.userId) != str(user.id)):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    return _job_to_dict(job)


@router.get("/jobs/{job_id}/download")
async def download_job(
    job_id: str,
    user: Annotated[UserExtra, Depends(get_current_user)],
):
    """Return a JSON {url} pointing at S3 (presigned, 15min) or, for legacy
    rows, return the PDF bytes inline. JSON form lets the SPA do an anchor
    click for cross-origin download without CORS preflight on S3."""
    job = await ResumeApplication.get(to_object_id(job_id))
    if job is None or (user.role != "admin" and str(job.userId) != str(user.id)):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")

    download_name = (job.pdfFilename or f"resume_{job.companyName}.pdf").split("/")[-1]

    if job.s3Key and s3_enabled():
        try:
            url = generate_presigned_get(job.s3Key, expires=900, download_filename=download_name)
            return {"url": url, "filename": download_name}
        except Exception as exc:  # noqa: BLE001
            log.exception("presign failed for job %s: %s", job_id, exc)

    if job.pdfData:
        pdf = base64.b64decode(job.pdfData)
        return Response(
            content=pdf,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="{download_name}"'},
        )
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="PDF not available")


@router.delete("/jobs/{job_id}")
async def delete_job(
    job_id: str,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    job = await ResumeApplication.get(to_object_id(job_id))
    if job is None or (user.role != "admin" and str(job.userId) != str(user.id)):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    await job.delete()
    return {"ok": True}


# ---------- Legacy history alias (kept for any older callers) ----------------


@router.get("/history")
async def history(
    user: Annotated[UserExtra, Depends(get_current_user)],
    accountId: Optional[str] = None,
) -> dict:
    query: dict = {} if user.role == "admin" else {"userId": user.id}
    if accountId:
        query["accountId"] = to_object_id(accountId)
    rows = await ResumeApplication.find(query).sort("-createdAt").to_list()
    return {"applications": [doc_to_dict(r) for r in rows]}
