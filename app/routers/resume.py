from typing import Annotated, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Response, status
from pydantic import BaseModel, Field
from pymongo.errors import DuplicateKeyError

from app.deps import get_current_user
from app.models import Account, ResumeApplication, UserExtra
from app.resume_service import generate_resume
from app.routers._util import doc_to_dict, to_object_id

router = APIRouter(prefix="/resume", tags=["resume"])

MAX_JD_BYTES = 50_000


class GenerateBody(BaseModel):
    accountId: str
    company: str = Field(min_length=1)
    jobDescription: str = Field(min_length=1)


async def _load_owned_account(account_id: str, user: UserExtra) -> Account:
    account = await Account.get(to_object_id(account_id))
    is_admin = user.role == "admin"
    if account is None or (not is_admin and str(account.createdBy) != str(user.id)):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    return account


@router.post("/generate")
async def generate(
    body: GenerateBody,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> Response:
    if len(body.jobDescription.encode("utf-8")) > MAX_JD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"jobDescription exceeds {MAX_JD_BYTES} bytes",
        )

    account = await _load_owned_account(body.accountId, user)

    # Resume generation needs at least an experience blob to produce useful output.
    if not (account.experience or "").strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Account is missing the 'experience' field — fill it on the Accounts page first.",
        )

    company = body.company.strip()
    company_lower = company.lower()

    existing = await ResumeApplication.find_one(
        {
            "userId": user.id,
            "accountId": account.id,
            "companyNameLower": company_lower,
        }
    )
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="You've already applied for this company with this account.",
        )

    result = await generate_resume(account, body.jobDescription, company)

    application = ResumeApplication(
        userId=user.id,
        accountId=account.id,
        companyName=company,
        companyNameLower=company_lower,
        jobDescription=body.jobDescription,
        s3Key=result.s3_key,
        s3Url=result.s3_url,
    )
    try:
        await application.insert()
    except DuplicateKeyError:
        pass

    headers = {
        "Content-Disposition": f'attachment; filename="resume_{company.replace(" ", "_")}.pdf"',
    }
    if result.s3_url:
        headers["X-Resume-URL"] = result.s3_url

    return Response(content=result.pdf_bytes, media_type="application/pdf", headers=headers)


class ScreeningBody(BaseModel):
    accountId: str
    jobDescription: Optional[str] = ""
    questions: List[str]
    guidelines: Optional[str] = None
    guidelinesMode: Optional[str] = None
    saveGuidelines: bool = False
    useGeneratedResumeContext: bool = False


@router.post("/screening-answers")
async def screening_answers(
    body: ScreeningBody,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    """Generate answers to job application screening questions for an account.

    `useGeneratedResumeContext`: when true, prefers `account.previewHtml` (the
    last AI-rendered resume body) over raw experience as context. Combined
    with the resume-then-screening flow, this means screening answers reflect
    the *generated* resume's bullets, not just the raw account experience.
    """
    import json
    import re
    from openai import AsyncOpenAI
    from app.config import get_settings

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

    if body.saveGuidelines:
        if body.guidelines is not None:
            account.screeningPrompt = body.guidelines
        if body.guidelinesMode in ("markdown", "plaintext"):
            account.screeningPromptMode = body.guidelinesMode  # type: ignore[assignment]
        await account.save()

    guidelines = body.guidelines if body.guidelines is not None else (account.screeningPrompt or "")

    settings = get_settings()
    if not settings.OPENAI_API_KEY:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="OPENAI_API_KEY not configured",
        )

    use_resume = body.useGeneratedResumeContext and bool((account.previewHtml or "").strip())

    prompt_parts = [
        "You answer job-application screening questions on behalf of a candidate.",
        f"Candidate name: {account.name}.",
        f"Candidate email: {account.email}." if account.email else "",
        f"Candidate education: {account.education}." if account.education else "",
    ]
    if use_resume:
        prompt_parts.append(
            "The candidate's tailored resume HTML for this role is below. Treat it as the source "
            "of truth for what they emphasize and how they describe their work:\n"
            f"{account.previewHtml}"
        )
    elif (account.experience or "").strip():
        prompt_parts.append(f"Candidate experience:\n{account.experience}")

    if (body.jobDescription or "").strip():
        prompt_parts.append(f"Job description:\n{body.jobDescription}")
    if guidelines.strip():
        prompt_parts.append(f"Answer guidelines:\n{guidelines}")

    indexed = "\n".join(f"{i + 1}. {q}" for i, q in enumerate(questions))
    prompt_parts.append(
        "Answer each numbered screening question below in the candidate's voice. "
        "Be concise, specific, grounded in the provided context. If a question cannot be "
        "answered from the context, say so briefly rather than fabricating details."
    )
    prompt_parts.append(f"Questions:\n{indexed}")
    prompt_parts.append(
        f"Return a JSON object of the exact form: {{\"answers\": [\"...\", \"...\"]}} "
        f"with exactly {len(questions)} entries in the same order as the numbered questions. "
        "Do not include any text outside the JSON."
    )

    client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
    response = await client.responses.create(
        model=settings.RESUME_OPENAI_MODEL,
        input="\n\n".join(p for p in prompt_parts if p),
    )
    raw = response.output_text or ""

    answers: List[str] = []
    try:
        # GPT may wrap JSON in code fences; strip them defensively.
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw.strip(), flags=re.MULTILINE)
        parsed = json.loads(cleaned)
        if isinstance(parsed, dict) and isinstance(parsed.get("answers"), list):
            answers = [str(a) for a in parsed["answers"]]
    except (json.JSONDecodeError, ValueError):
        pass

    if not answers:
        # Fallback: split numbered output into one entry per question.
        chunks = re.split(r"^\s*\d+\.\s*", raw, flags=re.MULTILINE)
        answers = [c.strip() for c in chunks if c.strip()]

    while len(answers) < len(questions):
        answers.append("")
    answers = answers[: len(questions)]

    pairs = [{"question": q, "answer": a} for q, a in zip(questions, answers)]
    return {"pairs": pairs}


@router.post("/preview-html")
async def preview_html(
    body: dict,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    """Generate just the OpenAI HTML body (no PDF, no S3, no application record)
    so the structured-mode preview pane can cache and re-render locally.

    Body: { accountId: str, jobDescription?: str }
    """
    from app.resume_service import _build_prompt, _openai_html

    account_id = body.get("accountId")
    if not account_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="accountId required")

    account = await _load_owned_account(account_id, user)

    if not (account.experience or "").strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Account is missing the 'experience' field — fill it on the Accounts page first.",
        )

    jd = body.get("jobDescription") or (
        "Senior Software Engineer role focused on full-stack product engineering "
        "with strong React, TypeScript, FastAPI, and PostgreSQL experience."
    )

    prompt = _build_prompt(account, jd)
    html_content = await _openai_html(prompt)

    account.previewHtml = html_content
    await account.save()
    return {"html": html_content}


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
