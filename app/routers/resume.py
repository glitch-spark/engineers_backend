import base64
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated, Any, List, Optional

from beanie import PydanticObjectId
from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field

from app import ai_usage
from app.config import get_settings
from app.cover_letter import generate_cover_letter as write_cover_letter
from app import resume_queue
from app.deps import get_current_user
from app.models import Account, ResumeApplication, UserExtra
from app.models.resume_application import (
    COVER_LETTER_HISTORY_MAX,
    CoverLetterVersion,
    ScreeningPair as ScreeningPairModel,
)
from app.llm_router import complete_screening, parse_screening_answers
from app.model_catalog import (
    PROVIDER_LABEL,
    Task,
    available_models,
    default_model_id,
    public_view,
)
from app.resume_service import (
    generate_resume,
    generate_presigned_get,
    s3_enabled,
    trim_jd,
)
from app.resume_prompts import default_prompt_body, find_prompt, unit_template
from app.routers._util import doc_to_dict, load_owned_account, require_model, to_object_id

router = APIRouter(prefix="/resume", tags=["resume"])
log = logging.getLogger(__name__)

ARCHIVED_TEXT_DETAIL = (
    "This resume is past the retention window: its job description and resume text were archived, "
    "so it can't answer questions or write a cover letter. Generate a new resume for this job."
)

_SAFE_FILENAME_SEG = re.compile(r"[^A-Za-z0-9 _\-().]+")


def _safe_filename_segment(s: str) -> str:
    return _SAFE_FILENAME_SEG.sub("_", (s or "").strip()).strip() or "untitled"


def _build_filename(profile_name: str, company: str) -> str:
    """{Profile Name}/Resume ({Company Name}).pdf — slash kept as a path
    segment so S3 archives nest cleanly per profile."""
    return f"{_safe_filename_segment(profile_name)}/Resume ({_safe_filename_segment(company)}).pdf"


_load_owned_account = load_owned_account  # shared with app/routers/prompt_eval.py


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
    # Opt-in cover letter for this submission only.
    generateCoverLetter: bool = False
    # Catalog model ids (GET /resume/models); omitted/blank = server default for that task.
    resumeModel: Optional[str] = None
    coverLetterModel: Optional[str] = None
    screeningModel: Optional[str] = None
    # Optional one-line angle for the cover letter.
    coverLetterHook: Optional[str] = Field(default=None, max_length=500)
    # The profile's resume unit (template + prompt) to generate with; omitted = the default unit, as before.
    promptId: Optional[str] = None


@router.post("/generate", status_code=status.HTTP_202_ACCEPTED)
async def generate(
    body: GenerateBody,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    """Queue a resume-generation job and return immediately.

    The queue worker (app/resume_queue.py) runs the LLM + PDF + optional screening/cover-letter steps with
    bounded concurrency. Frontend polls GET /resume/jobs to follow progress.
    """
    resume_model = require_model("resume", body.resumeModel, user)
    cover_letter_model = require_model("cover_letter", body.coverLetterModel, user)
    screening_model = require_model("screening", body.screeningModel, user)
    account = await _load_owned_account(body.accountId, user)
    with_unit: dict = {"prompt_body": body.promptBody}
    if body.promptId:
        # As Job Applies tailoring: the unit's prompt (a one-off promptBody still wins) and its template.
        unit = find_prompt(account, body.promptId)
        if unit is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="That resume unit is no longer on this profile.")
        if not unit.templateHtml:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="This resume unit has no HTML template.")
        with_unit = {"prompt_body": (body.promptBody or "").strip() or unit.body or None, "prompt_id": unit.id,
                     "prompt_name": unit.name, "template_html": unit.templateHtml,
                     "template_name": unit.templateName or "HTML template"}
    job = await resume_queue.enqueue_generation(
        user=user,
        account=account,
        jd=body.jobDescription,
        company=body.company,
        job_url=body.jobUrl,
        questions=body.questions,
        **with_unit,
        cover_letter=body.generateCoverLetter,
        resume_model=resume_model,
        cover_letter_model=cover_letter_model,
        screening_model=screening_model,
        cover_letter_hook=body.coverLetterHook,
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
    generate_cover_letter: bool = False,
    tailor_jd: Optional[str] = None,
) -> None:
    """Background orchestrator: resume → optional screening + cover letter,
    persisting state at each step."""
    started = time.monotonic()
    job = await ResumeApplication.get(job_id)
    if job is None:
        return
    account = await Account.get(account_id)
    if account is None:
        await _mark_failed(job, "account no longer exists", started)
        return
    owner = await UserExtra.get(user_id)
    # The resume unit the job was queued with (Job Applies): its HTML template and page format. No unit, or a unit
    # deleted / emptied since → the profile's (default unit's) template.
    unit = find_prompt(account, job.promptId) if job.promptId else None
    template_html, page_format = unit_template(unit) if unit is not None and unit.templateHtml else (None, None)

    # Resolve resume content-prompt: per-generation > account > user > default.
    resolved_prompt_body = (prompt_body_override or "").strip()
    if not resolved_prompt_body:
        resolved_prompt_body = default_prompt_body(account).strip()
    if not resolved_prompt_body:
        resolved_prompt_body = (owner.resumePromptBody or "").strip() if owner else ""
    # Empty → no content guidance sent to the LLM (system rules only).

    # Resolve screening prompt: account > user. Empty → no guidance sent.
    resolved_screening = (account.screeningPrompt or "").strip()
    if not resolved_screening:
        resolved_screening = (owner.screeningPromptBody or "").strip() if owner else ""

    try:
        # ---- step 1: generate resume ----
        job.status = "in_progress"
        job.step = "generating_resume"
        job.startedAt = datetime.now(timezone.utc)
        await resume_queue.persist_job(job)

        result = await generate_resume(
            account, (tailor_jd or "").strip() or jd, company, prompt_body=resolved_prompt_body or None, owner=owner,
            job_id=str(job.id), model_id=job.resumeModelId, template_html=template_html, page_format=page_format,
        )
        _add_cost(job, result.cost_usd)
        job.inputTokens = result.input_tokens
        job.outputTokens = result.output_tokens
        job.reasoningTokens = result.reasoning_tokens
        job.plainText = result.plain_text
        job.resumeLlmProvider = result.llm_provider
        job.resumeLlmModel = result.llm_model
        job.resumeLlmFallbackUsed = result.llm_fallback_used
        job.resumeLlmFallbackReason = result.llm_fallback_reason

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
        await resume_queue.persist_job(job)
        log.info(
            "job %s pdf persisted: %d bytes (s3_key=%s)",
            job.id, len(result.pdf_bytes), result.s3_key or "<inline>",
        )

        # ---- step 3: screening (optional) ----
        if questions:
            job.step = "generating_answers"
            await resume_queue.persist_job(job)

            screening_result = await _run_screening(
                account=account,
                jd=jd,
                questions=questions,
                screening_prompt=resolved_screening,
                owner=owner,
                model_id=job.screeningModelId,
            )
            _add_cost(job, screening_result.cost_usd)
            pairs = screening_result.pairs
            job.screeningLlmProvider = screening_result.llm_provider
            job.screeningLlmModel = screening_result.llm_model
            job.screeningLlmFallbackUsed = screening_result.llm_fallback_used
            job.screeningLlmFallbackReason = screening_result.llm_fallback_reason

            # Coerce dict -> ScreeningPair so Beanie/Pydantic doesn't mis-validate on save.
            job.screeningPairs = [ScreeningPairModel(**p) for p in pairs]

        # ---- step 4: cover letter (optional) ----
        if generate_cover_letter:
            cl_prompt = (account.coverLetterPrompt or "").strip()
            if not cl_prompt and owner:
                cl_prompt = (getattr(owner, "coverLetterPromptBody", None) or "").strip()
            try:
                cl = await write_cover_letter(
                    resume_text=job.plainText or "",
                    job_description=jd,
                    company=company,
                    prompt_body=cl_prompt,
                    user=owner,
                    model_id=job.coverLetterModelId,
                    hook=job.coverLetterHook,
                )
                job.coverLetterText = cl.text
                job.coverLetterLlmProvider = cl.provider
                job.coverLetterLlmModel = cl.model
                job.coverLetterLlmFallbackUsed = cl.fallback_used
                job.coverLetterLlmFallbackReason = cl.fallback_reason
                _add_cost(job, cl.cost_usd)
            except Exception as exc:  # noqa: BLE001 — non-fatal
                log.warning("cover-letter generation failed for job %s: %s", job_id, exc)

        # ---- match score (Job Applies): the tailored resume against its job, shown beside it ----
        from app.job_apply import tailored_score

        job.matchScore = await tailored_score.score_job(job)

        # ---- done ----
        job.status = "completed"
        job.step = "done"
        job.completedAt = datetime.now(timezone.utc)
        job.executionMs = int((time.monotonic() - started) * 1000)
        await resume_queue.persist_job(job)
        # Job Applies: a sheet row exported before this resume was ready gets its link now (never fails the job).
        from app.job_apply import sheet_links

        await sheet_links.fill_link(job)

        # Pipeline is now driven entirely by email sync — completed resume
        # generations no longer auto-create Application rows. Users land in
        # the Pipeline board only when Gmail surfaces a confirmation,
        # recruiter reach-out, or interview email tied to a real company.

    except resume_queue.Superseded:
        log.info("resume job %s was re-queued elsewhere; this run stops", job_id)
    except Exception as exc:  # noqa: BLE001 — capture *any* failure on the job
        log.exception("resume job %s failed", job_id)
        await _mark_failed(job, str(exc), started)


def _add_cost(job: ResumeApplication, cost: Optional[float]) -> None:
    """Add one step's spend to the job's running total (unknown prices add nothing)."""
    if cost is not None:
        job.estimatedCostUsd = (job.estimatedCostUsd or 0.0) + cost


async def run_job_doc(job: ResumeApplication) -> None:
    """Run a claimed queue job with the inputs stored on it (used by app/resume_queue.py)."""
    with ai_usage.usage_scope(user_id=job.userId, ref_id=job.id):
        await _run_job(
            job_id=job.id,
            account_id=job.accountId,
            user_id=job.userId,
            jd=job.jobDescription,
            company=job.companyName,
            questions=list(job.screeningQuestions or []),
            prompt_body_override=job.promptOverride,
            generate_cover_letter=bool(job.generateCoverLetter),
            tailor_jd=job.tailorJobDescription,
        )


async def _mark_failed(job: ResumeApplication, message: str, started: float) -> None:
    job.status = "failed"
    job.errorMessage = message[:1000]
    job.completedAt = datetime.now(timezone.utc)
    job.executionMs = int((time.monotonic() - started) * 1000)
    try:
        await resume_queue.persist_job(job)
    except resume_queue.Superseded:
        pass
    except Exception:  # noqa: BLE001
        log.exception("could not persist failure for job %s", job.id)


def _reset_failed_job_fields(job: ResumeApplication) -> None:
    """Clear outputs/errors and return a failed job to queued for re-run."""
    job.status = "queued"
    job.step = "queued"
    job.errorMessage = None
    job.startedAt = None
    job.completedAt = None
    job.executionMs = None
    job.pdfData = None
    job.s3Key = None
    job.s3Url = None
    job.plainText = None
    job.coverLetterText = None
    job.coverLetterLlmProvider = None
    job.coverLetterLlmModel = None
    job.coverLetterLlmFallbackUsed = None
    job.coverLetterLlmFallbackReason = None
    job.coverLetterHistory = []
    job.screeningPairs = []
    job.inputTokens = None
    job.outputTokens = None
    job.reasoningTokens = None
    job.resumeLlmProvider = None
    job.resumeLlmModel = None
    job.resumeLlmFallbackUsed = None
    job.resumeLlmFallbackReason = None
    job.screeningLlmProvider = None
    job.screeningLlmModel = None
    job.screeningLlmFallbackUsed = None
    job.screeningLlmFallbackReason = None
    job.estimatedCostUsd = None


# ---------- Screening (extracted helper, reused by job + standalone endpoint) -


@dataclass
class ScreeningResult:
    pairs: List[dict]
    llm_provider: str
    llm_model: str
    llm_fallback_used: bool
    llm_fallback_reason: Optional[str] = None
    cost_usd: Optional[float] = None


def _build_screening_prompt(
    account: Account,
    jd: str,
    questions: List[str],
    screening_prompt: str,
    resume_text: Optional[str] = None,
) -> str:
    candidate_text = (resume_text or "").strip()
    candidate_source = "tailored resume"
    if not candidate_text:
        template_html = (account.styleTemplate or "").strip()
        if template_html:
            from bs4 import BeautifulSoup
            candidate_text = re.sub(
                r"\n{3,}", "\n\n",
                BeautifulSoup(template_html, "html.parser").get_text("\n").strip(),
            )
            candidate_source = "resume template"

    parts: List[str] = [
        "You answer job-application screening questions on behalf of a candidate.",
        f"Profile: {account.name}.",
    ]
    if candidate_text:
        parts.append(
            f"Candidate context (extracted from their {candidate_source}):\n"
            + candidate_text
        )

    if (jd or "").strip():
        parts.append(f"Job description:\n{trim_jd(jd)}")

    indexed = "\n".join(f"{i + 1}. {q}" for i, q in enumerate(questions))
    if (screening_prompt or "").strip():
        parts.append(screening_prompt.strip())
    parts.append(f"Questions:\n{indexed}")
    parts.append(
        f"Return a JSON object of the exact form: {{\"answers\": [\"...\", \"...\"]}} "
        f"with exactly {len(questions)} entries in the same order as the numbered questions. "
        "Each entry in \"answers\" MUST be a plain string (the answer text only) — "
        "not an object, not metadata, not {{question, answer}}. "
        "Do not include any text outside the JSON."
    )
    return "\n\n".join(p for p in parts if p)


async def _run_screening(
    account: Account,
    jd: str,
    questions: List[str],
    screening_prompt: str,
    resume_text: Optional[str] = None,
    owner: Optional[UserExtra] = None,
    model_id: Optional[str] = None,
) -> ScreeningResult:
    """Run screening on `model_id` (or the task default)."""
    prompt = _build_screening_prompt(
        account, jd, questions, screening_prompt, resume_text=resume_text
    )

    llm_result = await complete_screening(prompt, owner, questions, model_id=model_id)

    answers = parse_screening_answers(llm_result.text, questions)
    pairs = [{"question": q, "answer": a} for q, a in zip(questions, answers)]
    return ScreeningResult(
        pairs=pairs,
        llm_provider=llm_result.provider,
        llm_model=llm_result.model,
        llm_fallback_used=llm_result.fallback_used,
        llm_fallback_reason=llm_result.fallback_reason,
        cost_usd=llm_result.cost_usd,
    )


class ScreeningBody(BaseModel):
    accountId: str
    jobDescription: Optional[str] = ""
    questions: List[str]
    model: Optional[str] = None  # catalog id (GET /resume/models); blank = server default


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

    model_id = require_model("screening", body.model, user)
    account = await _load_owned_account(body.accountId, user)
    if not (account.styleTemplate or "").strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Profile has no HTML template yet — upload one on the Resume Styles tab first.",
        )

    screening = (account.screeningPrompt or "").strip()
    if not screening:
        screening = (user.screeningPromptBody or "").strip()

    result = await _run_screening(
        account=account,
        jd=body.jobDescription or "",
        questions=questions,
        screening_prompt=screening,
        owner=user,
        model_id=model_id,
    )
    return {"pairs": result.pairs}


class JobAskBody(BaseModel):
    questions: List[str] = Field(min_length=1, max_length=10)
    model: Optional[str] = None  # catalog id (GET /resume/models); blank = server default


@router.post("/jobs/{job_id}/ask")
async def ask_job_screening(
    job_id: str,
    body: JobAskBody,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    """Ad-hoc screening Q&A against an existing generated resume — uses the
    job's stored JD + the profile's screening prompt. Appends the new pairs
    to the job so future opens show them. Owner-only (admin too)."""
    oid = to_object_id(job_id)
    job = await ResumeApplication.get(oid)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    is_admin = user.role == "admin"
    if not is_admin and str(job.userId) != str(user.id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")
    if job.accountId is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="job has no profile")
    if getattr(job, "textArchivedAt", None) is not None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=ARCHIVED_TEXT_DETAIL)
    account = await Account.get(job.accountId)
    if account is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="profile no longer exists")
    owner = await UserExtra.get(job.userId)
    model_id = require_model("screening", body.model, owner)

    questions = [q.strip() for q in body.questions if q and q.strip()]
    if not questions:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="At least one non-empty question is required")

    # Same precedence as initial generation: account > user > empty.
    screening_prompt = (account.screeningPrompt or "").strip()
    if not screening_prompt and owner:
        screening_prompt = (owner.screeningPromptBody or "").strip()

    with ai_usage.usage_scope(user_id=job.userId, ref_id=job_id):
        screening_result = await _run_screening(
            account=account,
            jd=job.jobDescription or "",
            questions=questions,
            screening_prompt=screening_prompt,
            resume_text=job.plainText or "",
            owner=owner,
            model_id=model_id,
        )

    _add_cost(job, screening_result.cost_usd)
    job.screeningLlmProvider = screening_result.llm_provider
    job.screeningLlmModel = screening_result.llm_model
    job.screeningLlmFallbackUsed = screening_result.llm_fallback_used
    job.screeningLlmFallbackReason = screening_result.llm_fallback_reason
    job.screeningPairs = [
        *(job.screeningPairs or []),
        *[ScreeningPairModel(**p) for p in screening_result.pairs],
    ]
    await job.save()
    return {"pairs": screening_result.pairs}


class CoverLetterRegenBody(BaseModel):
    model: Optional[str] = None  # catalog id (GET /resume/models); blank = server default
    hook: Optional[str] = Field(default=None, max_length=500)  # None = keep the job's stored hook


@router.post("/jobs/{job_id}/cover-letter")
async def regenerate_cover_letter(
    job_id: str,
    body: CoverLetterRegenBody,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    """Write the job's cover letter again (optionally on another model). The previous letter is
    kept in `coverLetterHistory` (newest first, capped). Owner-only (admin too)."""
    oid = to_object_id(job_id)
    job = await ResumeApplication.get(oid)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    if user.role != "admin" and str(job.userId) != str(user.id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")
    owner = await UserExtra.get(job.userId)
    model_id = require_model("cover_letter", body.model, owner)
    if getattr(job, "textArchivedAt", None) is not None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=ARCHIVED_TEXT_DETAIL)
    if not (job.plainText or "").strip() or not (job.jobDescription or "").strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This job has no resume text or job description to write a letter from.",
        )

    account = await Account.get(job.accountId) if job.accountId else None
    prompt_body = (getattr(account, "coverLetterPrompt", None) or "").strip()
    if not prompt_body and owner:
        prompt_body = (getattr(owner, "coverLetterPromptBody", None) or "").strip()
    hook = (body.hook or "").strip() if body.hook is not None else (job.coverLetterHook or "")

    try:
        with ai_usage.usage_scope(user_id=job.userId, ref_id=job_id):
            cl = await write_cover_letter(
                resume_text=job.plainText or "",
                job_description=job.jobDescription or "",
                company=job.companyName,
                prompt_body=prompt_body,
                user=owner,
                model_id=model_id,
                hook=hook or None,
            )
    except Exception as exc:  # noqa: BLE001 — surface any provider failure; the job stays as it was
        log.warning("cover-letter regeneration failed for job %s: %s", job_id, exc)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Cover letter generation failed: {exc}"
        )

    history = list(job.coverLetterHistory or [])
    if (job.coverLetterText or "").strip():
        history.insert(
            0,
            CoverLetterVersion(
                text=job.coverLetterText,
                provider=job.coverLetterLlmProvider or "",
                model=job.coverLetterLlmModel or "",
                createdAt=datetime.now(timezone.utc),
            ),
        )
    job.coverLetterHistory = history[:COVER_LETTER_HISTORY_MAX]
    job.coverLetterText = cl.text
    job.coverLetterLlmProvider = cl.provider
    job.coverLetterLlmModel = cl.model
    job.coverLetterLlmFallbackUsed = cl.fallback_used
    job.coverLetterLlmFallbackReason = cl.fallback_reason
    if model_id:
        job.coverLetterModelId = model_id
    if body.hook is not None:
        job.coverLetterHook = hook or None
    _add_cost(job, cl.cost_usd)
    await job.save()
    return {
        "coverLetterText": job.coverLetterText,
        "coverLetterLlmProvider": job.coverLetterLlmProvider,
        "coverLetterLlmModel": job.coverLetterLlmModel,
        "coverLetterLlmFallbackUsed": job.coverLetterLlmFallbackUsed,
        "coverLetterLlmFallbackReason": job.coverLetterLlmFallbackReason,
        "estimatedCostUsd": job.estimatedCostUsd,
        "coverLetterHistory": [v.model_dump(mode="json") for v in job.coverLetterHistory],
    }


# ---------- Model catalog -----------------------------------------------------


@router.get("/models")
async def list_models(
    task: Task,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    """Models the caller can pick for `task`, with the default flagged and a typical per-run cost."""
    default_id = default_model_id(task, user)
    models = available_models(task, user)
    providers = list(dict.fromkeys(m.provider for m in models))  # configured providers, in picker order
    return {
        "task": task,
        "defaultId": default_id,
        "providers": [{"id": p, "label": PROVIDER_LABEL[p]} for p in providers],
        "models": [public_view(s, task, default_id) for s in models],
    }


# ---------- Job listing / polling / download ---------------------------------


_NO_LINK = {"jobApplyRowId": None, "jobApplyRunId": None, "jobApplyRunName": None}


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
        "source": job.source,
        "pdfFilename": job.pdfFilename,
        "s3Key": job.s3Key,
        "errorMessage": job.errorMessage,
        "executionMs": job.executionMs,
        "screeningQuestions": job.screeningQuestions,
        "screeningPairs": [p.model_dump() for p in job.screeningPairs],
        "jobDescription": job.jobDescription or "",
        "inputTokens": job.inputTokens,
        "outputTokens": job.outputTokens,
        "reasoningTokens": job.reasoningTokens,
        "coverLetterText": job.coverLetterText,
        "coverLetterLlmProvider": job.coverLetterLlmProvider,
        "coverLetterLlmModel": job.coverLetterLlmModel,
        "coverLetterLlmFallbackUsed": job.coverLetterLlmFallbackUsed,
        "coverLetterLlmFallbackReason": job.coverLetterLlmFallbackReason,
        "coverLetterHistory": [v.model_dump(mode="json") for v in job.coverLetterHistory],
        "resumeModelId": job.resumeModelId,
        "coverLetterModelId": job.coverLetterModelId,
        "screeningModelId": job.screeningModelId,
        "coverLetterHook": job.coverLetterHook,
        "estimatedCostUsd": job.estimatedCostUsd,
        "resumeLlmProvider": job.resumeLlmProvider,
        "resumeLlmModel": job.resumeLlmModel,
        "resumeLlmFallbackUsed": job.resumeLlmFallbackUsed,
        "resumeLlmFallbackReason": job.resumeLlmFallbackReason,
        "screeningLlmProvider": job.screeningLlmProvider,
        "screeningLlmModel": job.screeningLlmModel,
        "screeningLlmFallbackUsed": job.screeningLlmFallbackUsed,
        "screeningLlmFallbackReason": job.screeningLlmFallbackReason,
        "hasPdf": bool(job.s3Key) or bool(job.pdfData),
        "createdAt": job.createdAt.isoformat() if job.createdAt else None,
        "startedAt": job.startedAt.isoformat() if job.startedAt else None,
        "completedAt": job.completedAt.isoformat() if job.completedAt else None,
    }
    return d


async def _job_apply_links(jobs: list[ResumeApplication]) -> dict[str, dict]:
    """Per resume tailored from Job Applies: its job row, run and run name (run fields None once the run expired)."""
    from app.models.job_apply import JobApplyRow, JobApplyRun

    row_ids = list({j.jobApplyRowId for j in jobs if getattr(j, "jobApplyRowId", None)})
    if not row_ids:
        return {}
    rows = {
        r["_id"]: r["runId"]
        async for r in JobApplyRow.get_motor_collection().find({"_id": {"$in": row_ids}}, {"runId": 1})
    }
    runs = {
        r["_id"]: r.get("fileName") or "Job Applies run"
        async for r in JobApplyRun.get_motor_collection().find({"_id": {"$in": list(set(rows.values()))}}, {"fileName": 1})
    }
    out = {}
    for j in jobs:
        rid = getattr(j, "jobApplyRowId", None)
        if not rid:
            continue
        run_id = rows.get(rid)
        out[str(j.id)] = {
            "jobApplyRowId": str(rid),
            "jobApplyRunId": str(run_id) if run_id in runs else None,
            "jobApplyRunName": runs.get(run_id),
        }
    return out


@router.get("/jobs")
async def list_jobs(
    user: Annotated[UserExtra, Depends(get_current_user)],
    accountId: Optional[str] = None,
    company: Optional[str] = None,
    q: Optional[str] = None,
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
    # Free-text search across the stored JD only. Multi-word = AND (each
    # term must appear somewhere in the JD, case-insensitive).
    q_terms: list[str] = []
    if q and q.strip():
        q_terms = [t for t in re.split(r"\s+", q.strip()) if t]
        if q_terms:
            query["$and"] = [
                {"jobDescription": {"$regex": re.escape(t), "$options": "i"}}
                for t in q_terms
            ]
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

    def _snippet(jd: str) -> Optional[str]:
        """Pull ~160 chars of JD around the first matched term so the row
        can show the user *why* it matched."""
        if not jd or not q_terms:
            return None
        first_match: Optional[re.Match[str]] = None
        for t in q_terms:
            m = re.search(re.escape(t), jd, re.IGNORECASE)
            if m and (first_match is None or m.start() < first_match.start()):
                first_match = m
        if first_match is None:
            return None
        s = max(0, first_match.start() - 70)
        e = min(len(jd), first_match.end() + 90)
        out = jd[s:e].strip().replace("\n", " ")
        if s > 0:
            out = "…" + out
        if e < len(jd):
            out = out + "…"
        return out

    jobs_out = []
    links = await _job_apply_links(rows)
    for r in rows:
        d = {**_job_to_dict(r), **_NO_LINK, **links.get(str(r.id), {})}
        snip = _snippet(r.jobDescription or "")
        if snip:
            d["matchSnippet"] = snip
        jobs_out.append(d)

    total_pages = (total + limit - 1) // limit if limit else 0
    return {
        "jobs": jobs_out,
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
    links = await _job_apply_links([job])
    return {**_job_to_dict(job), **_NO_LINK, **links.get(str(job.id), {})}


_FOLDER_BAD = re.compile(r"[\\/\x00-\x1f<>:\"|?*]+")


def _safe_folder(name: str) -> str:
    """Filesystem-safe folder name derived from the company. Strips path
    separators + control chars + reserved Windows chars; trims dots/spaces."""
    s = _FOLDER_BAD.sub("_", (name or "").strip())
    s = s.strip(". ") or "company"
    return s[:120]


async def _fetch_pdf_bytes(job: ResumeApplication) -> bytes:
    """PDF bytes for a job — pulled from S3 when configured, else inline
    pdfData. Used by both single + bulk zip downloads."""
    if job.s3Key and s3_enabled():
        from app.resume_service import _s3_client
        s = get_settings()
        try:
            obj = _s3_client().get_object(Bucket=s.AWS_S3_BUCKET, Key=job.s3Key)
            return obj["Body"].read()
        except Exception as exc:  # noqa: BLE001
            log.exception("s3 get_object failed for job %s: %s", job.id, exc)
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="PDF fetch failed")
    if job.pdfData:
        return base64.b64decode(job.pdfData)
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="PDF not available")


def _build_zip(entries: list[tuple[str, bytes]]) -> bytes:
    """Build an in-memory zip with `(path, bytes)` pairs. Dedups colliding
    paths by appending ` (2)`, ` (3)`, ..."""
    import io
    import zipfile

    seen: dict[str, int] = {}
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, data in entries:
            base, _, ext = path.rpartition(".")
            if base == "":
                base, ext = path, ""
            final = path
            if path in seen:
                seen[path] += 1
                final = f"{base} ({seen[path]}).{ext}" if ext else f"{path} ({seen[path]})"
            else:
                seen[path] = 1
            zf.writestr(final, data)
    return buf.getvalue()


def _job_archive_path(job: ResumeApplication) -> str:
    """Folder path inside the archive (or on disk under the user-picked
    directory). Shape: `<ProfileName>/<CompanyName>` — both sanitized for
    filesystem safety."""
    profile = _safe_folder(job.profileName or "profile")
    company = _safe_folder(job.companyName or "company")
    return f"{profile}/{company}"


@router.get("/jobs/{job_id}/download")
async def download_job(
    job_id: str,
    user: Annotated[UserExtra, Depends(get_current_user)],
    format: str = "zip",
):
    """Default returns a ZIP containing `<profile>/<company>/Resume.pdf`.
    Pass `?format=pdf` for raw PDF bytes plus an `X-Folder: <profile>/<company>`
    header — used by the File System Access API path so the SPA can write
    nested folders directly into the user-picked directory."""
    job = await ResumeApplication.get(to_object_id(job_id))
    if job is None or (user.role != "admin" and str(job.userId) != str(user.id)):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")

    pdf = await _fetch_pdf_bytes(job)
    folder = _job_archive_path(job)
    if format == "pdf":
        return Response(
            content=pdf,
            media_type="application/pdf",
            headers={"Content-Disposition": f'attachment; filename="Resume.pdf"', "X-Folder": folder},
        )
    zip_bytes = _build_zip([(f"{folder}/Resume.pdf", pdf)])
    zip_name = f"{_safe_folder(job.companyName or 'company')}.zip"
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{zip_name}"'},
    )


class BulkDownloadBody(BaseModel):
    jobIds: List[str] = Field(min_length=1, max_length=100)


@router.post("/jobs/bulk-download")
async def bulk_download_jobs(
    body: BulkDownloadBody,
    user: Annotated[UserExtra, Depends(get_current_user)],
):
    """Stream a single ZIP containing one folder per selected job:
    `<company>/Resume.pdf`. Companies that repeat get suffixed " (2)", " (3)"
    inside the archive so nothing is overwritten."""
    oids = [to_object_id(jid) for jid in body.jobIds]
    is_admin = user.role == "admin"
    query: dict = {"_id": {"$in": oids}}
    if not is_admin:
        query["userId"] = user.id
    jobs = await ResumeApplication.find(query).to_list()
    if not jobs:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no jobs in scope")

    entries: list[tuple[str, bytes]] = []
    for job in jobs:
        try:
            pdf = await _fetch_pdf_bytes(job)
        except HTTPException:
            continue  # skip rows whose PDF isn't available
        entries.append((f"{_job_archive_path(job)}/Resume.pdf", pdf))
    if not entries:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="no downloadable PDFs in selection")

    zip_bytes = _build_zip(entries)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    zip_name = f"resumes-{stamp}.zip"
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{zip_name}"'},
    )


@router.post("/jobs/{job_id}/retry", status_code=status.HTTP_202_ACCEPTED)
async def retry_job(
    job_id: str,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    """Re-queue a failed job in place with its original JD / company / questions."""
    job = await ResumeApplication.get(to_object_id(job_id))
    if job is None or (user.role != "admin" and str(job.userId) != str(user.id)):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    if job.status != "failed":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only failed jobs can be retried",
        )
    if not job.accountId:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Job has no profile; cannot retry",
        )

    account = await _load_owned_account(str(job.accountId), user)
    if not (account.styleTemplate or "").strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No HTML template on this profile. Upload one on the Resume Styles tab first.",
        )

    _reset_failed_job_fields(job)
    job.attempts = 0
    job.leaseAt = None
    await job.save()
    resume_queue.wake()

    return {"jobId": str(job.id), "status": job.status}


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
