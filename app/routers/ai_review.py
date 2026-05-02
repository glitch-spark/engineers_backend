import json
from typing import Annotated, AsyncGenerator, Literal, Optional

from beanie import PydanticObjectId
from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from jose import JWTError
from pydantic import BaseModel, Field

from app.config import get_settings
from app.deps import get_current_user
from app.models import Account, AiReviewRun, GlobalPrompt, Interview, Skill, UserExtra
from app.routers._util import doc_to_dict, to_object_id
from app.security import decode_token

router = APIRouter(prefix="/ai-review", tags=["ai-review"])

MAX_CUSTOM_PROMPT_CHARS = 4000
MAX_RESUME_CHARS = 4000


async def _user_from_token(token: str) -> UserExtra:
    if not token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing token")
    try:
        payload = decode_token(token)
    except JWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token")
    sub = payload.get("sub")
    if not sub:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token payload")
    try:
        uid = PydanticObjectId(sub)
    except Exception:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token subject")
    user = await UserExtra.get(uid)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="user not found")
    return user


def _truncate(text: str, limit: int) -> str:
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + "\n\n[...truncated]"


async def _build_interview_contexts(interview_ids: list[PydanticObjectId]) -> list[dict]:
    interviews = await Interview.find({"_id": {"$in": interview_ids}}).to_list()
    by_id = {iv.id: iv for iv in interviews}
    ordered = [by_id[iid] for iid in interview_ids if iid in by_id]

    account_ids = list({iv.accountId for iv in ordered})
    accounts = await Account.find({"_id": {"$in": account_ids}}).to_list() if account_ids else []
    accs_by_id = {a.id: a for a in accounts}

    contexts: list[dict] = []
    for iv in ordered:
        acc = accs_by_id.get(iv.accountId)
        resume_markdown = ""
        resume_filename = None
        if acc and iv.resumeId:
            r = next((r for r in acc.resumes if r.id == iv.resumeId), None)
            if r:
                resume_filename = r.filename
                resume_markdown = _truncate(r.markdown or "", MAX_RESUME_CHARS)
        contexts.append({
            "candidate_name": acc.name if acc else None,
            "candidate_email": acc.email if acc else None,
            "address": getattr(acc, "address", None) if acc else None,
            "company_name": iv.companyName,
            "interviewer_name": iv.interviewerName,
            "applied_position": iv.appliedPosition,
            "main_tech_stack": iv.mainTechStack,
            "stage": iv.stage.value if iv.stage else None,
            "status": iv.status.value if iv.status else None,
            "scheduled_at": iv.scheduledAt.isoformat() if iv.scheduledAt else None,
            "resume_filename": resume_filename,
            "resume_markdown": resume_markdown,
            "transcript": iv.transcript or "",
            "note": iv.note or "",
        })
    return contexts


def _sse(data: dict, event: Optional[str] = None) -> str:
    out = ""
    if event:
        out += f"event: {event}\n"
    out += f"data: {json.dumps(data)}\n\n"
    return out


@router.get("/stream")
async def stream_ai_review(
    interview_ids: list[str] = Query(..., alias="interviewIds"),
    skill_id: Optional[str] = Query(None, alias="skillId"),
    custom_prompt: Optional[str] = Query(None, alias="customPrompt"),
    token: str = Query(...),
):
    user = await _user_from_token(token)
    is_admin = user.role == "admin"

    if not skill_id and not custom_prompt:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Provide skillId or customPrompt")
    if skill_id and custom_prompt:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Provide skillId or customPrompt, not both")
    if custom_prompt and len(custom_prompt) > MAX_CUSTOM_PROMPT_CHARS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"customPrompt exceeds {MAX_CUSTOM_PROMPT_CHARS} characters")

    if not interview_ids:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Select at least one interview")
    iv_oids = [to_object_id(i) for i in interview_ids]

    skill: Optional[Skill] = None
    if skill_id:
        skill = await Skill.get(to_object_id(skill_id))
        if skill is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Skill not found")
        n = len(iv_oids)
        if n < skill.minInterviews or n > skill.maxInterviews:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"This skill needs between {skill.minInterviews} and {skill.maxInterviews} interviews ({n} selected)",
            )

    interviews = await Interview.find({"_id": {"$in": iv_oids}}).to_list()
    if len(interviews) != len(iv_oids):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="One or more interviews not found")
    if not is_admin:
        for iv in interviews:
            if iv.createdBy != user.id:
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="You do not own all selected interviews")

    contexts = await _build_interview_contexts(iv_oids)

    global_doc = await GlobalPrompt.find_one(GlobalPrompt.key == "default")
    global_prefix = (global_doc.systemPrompt or "").strip() if global_doc else ""

    if skill:
        skill_part = (skill.systemPrompt or "").strip()
        system_prompt = f"{global_prefix}\n\n{skill_part}".strip() if global_prefix else skill_part
    else:
        custom = (custom_prompt or "").strip()
        system_prompt = f"{global_prefix}\n\n{custom}".strip() if global_prefix else custom

    if not system_prompt:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Resolved system prompt is empty")

    user_message = "Interviews:\n" + json.dumps(contexts, indent=2, ensure_ascii=False)

    settings = get_settings()
    openai_key = getattr(settings, "OPENAI_API_KEY", None)
    if not openai_key:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="OPENAI_API_KEY is not configured on the server")
    model_name = getattr(settings, "OPENAI_MODEL", "gpt-4o-mini")

    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=openai_key)

    async def event_stream() -> AsyncGenerator[str, None]:
        collected: list[str] = []
        prompt_tokens = 0
        completion_tokens = 0
        try:
            stream = await client.chat.completions.create(
                model=model_name,
                stream=True,
                stream_options={"include_usage": True},
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
            )
            async for chunk in stream:
                if chunk.usage is not None:
                    prompt_tokens = chunk.usage.prompt_tokens or 0
                    completion_tokens = chunk.usage.completion_tokens or 0
                if chunk.choices:
                    delta = chunk.choices[0].delta
                    text = getattr(delta, "content", None)
                    if text:
                        collected.append(text)
                        yield _sse({"delta": text})
        except Exception as exc:
            yield _sse({"message": str(exc)}, event="error")
            return

        run = AiReviewRun(
            interviewIds=iv_oids,
            skillId=skill.id if skill else None,
            customPrompt=custom_prompt if not skill else None,
            output="".join(collected),
            model=model_name,
            promptTokens=prompt_tokens,
            completionTokens=completion_tokens,
            ranBy=user.id,
        )
        await run.insert()
        yield _sse({"runId": str(run.id)}, event="done")

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.get("/runs")
async def list_runs(
    user: Annotated[UserExtra, Depends(get_current_user)],
    limit: int = Query(50, ge=1, le=200),
) -> dict:
    is_admin = user.role == "admin"
    query: dict = {} if is_admin else {"ranBy": user.id}
    docs = await AiReviewRun.find(query).sort([("createdAt", -1)]).limit(limit).to_list()
    return {"runs": [doc_to_dict(d) for d in docs]}


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=8000)


class InterviewChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(default_factory=list, max_length=40)
    rubric: bool = False  # if true, ask the model to also score a rubric


_BRIEF_REVIEW_PROMPT = (
    "You are an interview reviewer. Read the candidate's interview transcript, "
    "resume, and job context below. Produce a concise review that includes:\n"
    "- TL;DR verdict (Strong Hire / Hire / Lean Hire / No Hire / Strong No Hire) + 1-line reasoning.\n"
    "- 3-5 strengths (bullet points, each with a brief evidence quote).\n"
    "- 3-5 gaps or concerns (each with a brief evidence quote).\n"
    "- Suggested follow-up questions for the next round.\n"
    "Be specific. Cite the transcript when you make claims. If information is missing, say so."
)

_RUBRIC_PROMPT = (
    "\n\nAlso include a rubric scored 1-5 for: Communication, Technical Depth, "
    "Problem Solving, Culture Fit, Coding (if applicable), System Design (if applicable). "
    "Format as a markdown table with one short justification per row."
)


@router.post("/interview/{iv_id}/chat")
async def interview_chat(
    iv_id: str,
    body: InterviewChatRequest,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    oid = to_object_id(iv_id)
    iv = await Interview.get(oid)
    if iv is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="interview not found")
    if user.role != "admin" and iv.createdBy != user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")

    contexts = await _build_interview_contexts([oid])
    global_doc = await GlobalPrompt.find_one(GlobalPrompt.key == "default")
    global_prefix = (global_doc.systemPrompt or "").strip() if global_doc else ""

    system_prompt = _BRIEF_REVIEW_PROMPT
    if body.rubric:
        system_prompt += _RUBRIC_PROMPT
    if global_prefix:
        system_prompt = f"{global_prefix}\n\n{system_prompt}"

    context_blob = "Interview context (JSON):\n" + json.dumps(contexts[0], indent=2, ensure_ascii=False)

    settings = get_settings()
    openai_key = getattr(settings, "OPENAI_API_KEY", None)
    if not openai_key:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="OPENAI_API_KEY is not configured")
    model_name = getattr(settings, "OPENAI_MODEL", "gpt-4o-mini")

    from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=openai_key)
    messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": context_blob},
    ]
    if not body.messages:
        # First call — kick off the brief review.
        messages.append({"role": "user", "content": "Please produce the brief review now."})
    else:
        for m in body.messages:
            messages.append({"role": m.role, "content": m.content})

    try:
        completion = await client.chat.completions.create(
            model=model_name,
            messages=messages,
        )
    except Exception as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))

    choice = completion.choices[0] if completion.choices else None
    reply = (choice.message.content if choice and choice.message else "") or ""
    usage = completion.usage
    return {
        "reply": reply,
        "model": model_name,
        "promptTokens": usage.prompt_tokens if usage else 0,
        "completionTokens": usage.completion_tokens if usage else 0,
    }


@router.get("/runs/{run_id}")
async def get_run(
    run_id: str,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    oid = to_object_id(run_id)
    doc = await AiReviewRun.get(oid)
    if doc is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    if user.role != "admin" and doc.ranBy != user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not your run")
    return doc_to_dict(doc)
