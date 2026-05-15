import json
from typing import Annotated, AsyncGenerator, Literal, Optional

from beanie import PydanticObjectId
from fastapi import APIRouter, Body, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from jose import JWTError
from pydantic import BaseModel, Field

from app.config import get_settings
from app.prompts import FAILURE_MODES, STAGE_DRILL_BANK, STAGE_LABELS
from app.deps import get_current_user
from app.models import Account, AiReviewRun, GlobalPrompt, Interview, InterviewChatLog, Skill, UserExtra
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
        primary_contact = ""
        if acc is not None:
            primary_contact = next(
                (ci.display for ci in (acc.contactItems or []) if ci.show and (ci.display or "").strip()),
                "",
            )
        contexts.append({
            "candidate_name": acc.name if acc else None,
            "candidate_email": primary_contact or None,
            "address": None,
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


_BRIEF_REVIEW_PROMPT = """\
You are an interview coach reviewing a transcript on behalf of the candidate.
The audience IS the candidate. Write to them directly as "you" — not about them
in third person. Goal: help them understand exactly what went well, what didn't,
and what to fix before their next interview at the SAME stage.

Be direct. Reassurance is less useful than truth. If they bombed, say so.

Output (markdown), in this order:

## Verdict
3-4 sentences. Honest read of overall performance against what this stage tests.
End with explicit signal: "on track for offer" / "borderline" / "below the bar".

## What you did well
2-4 bullets. Each cites a specific moment + one-line reason it landed (clear
structure, right depth, good clarifying question, real-world example, etc.).

## What hurt you
Do NOT review every question. Only call out answers with a notable failure mode.
Use these triggers — name the mode explicitly:

{failure_modes}
For each call-out:
1. Name the moment (paraphrase or quote ~10 words).
2. Name the failure mode (from the list).
3. Give one concrete fix in candidate's voice ("next time, lead with the
   constraint check before designing").

Cap: 2-5 call-outs. Pick the highest-leverage, not all of them.

## How to prep for the next {stage_label} interview
Stage-aware. Specific, time-boxed, drillable. NO generic advice
("study system design", "practice more"). Replace with:

- name the topic + drill volume / time budget
- name the language idiom or building block they tripped on
- name 1-2 concrete exercises (e.g. "design URL shortener focusing on
  shard rebalancing in 45 min, write down the 3 hardest tradeoffs you hit")
- if behavioral: name the STAR-component skipped (Situation/Task/Action/
  Result/Reflection) and the story to pre-write

Pick suggestions from the **stage drill bank** below where applicable. Adapt
to the candidate's specific gaps from the transcript — never copy verbatim.

If they crushed it, still give ONE stretch goal — what pushes from
"Hire" to "Strong Hire" at this stage.

## Keep doing
1-2 bullets on patterns to retain. Prevents over-correction.

Voice + format rules:
- Second person ("you"). Coach tone, direct.
- Cite concrete moments with short verbatim quotes when they help.
- Skip "perhaps consider" hedging. Use "do this" / "skip this" / "rewrite as".
- If transcript is partial or unclear, say so. Don't fabricate.
- Skip a section entirely if there's nothing meaningful to put in it.
- No emojis, no exclamation marks, no closing reassurance ("you've got this!").
"""

_RUBRIC_PROMPT = """\

## Rubric
Score 1-5 with one-line justification per row. Format as markdown table.
Include rows that apply to this stage; skip rows that don't (e.g. no Coding
row for a behavioral panel).

| Dimension | Score | Why |
|---|---|---|
| Communication | x/5 | ... |
| Technical Depth | x/5 | ... |
| Problem Solving | x/5 | ... |
| Culture Fit | x/5 | ... |
| Coding | x/5 | ... |
| System Design | x/5 | ... |
"""



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

    stage_key = iv.stage.value if iv.stage else None
    stage_label = STAGE_LABELS.get(stage_key, "next-stage")
    position = iv.appliedPosition or "(unspecified position)"
    company = iv.companyName or "(unspecified company)"

    header = (
        f"Interview meta — stage: {stage_label} · position: {position} · company: {company}.\n"
        "Tailor every section to this stage. Skip sections that don't apply.\n\n"
    )

    system_prompt = header + _BRIEF_REVIEW_PROMPT.format(
        stage_label=stage_label,
        failure_modes=FAILURE_MODES,
    )
    drill = STAGE_DRILL_BANK.get(stage_key)
    if drill:
        system_prompt += (
            f"\n\nStage drill bank for {stage_label} (adapt to candidate gaps; do not copy verbatim):\n"
            f"{drill}\n"
        )
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

    # Persist user follow-up question (skip the initial auto-brief which has no
    # user question). Each turn = its own doc so we can mine question patterns
    # later for system-prompt tuning + follow-up suggestion generation.
    last_user = next((m for m in reversed(body.messages) if m.role == "user"), None)
    if last_user:
        try:
            await InterviewChatLog(
                interviewId=oid,
                userId=user.id,
                question=last_user.content,
                answer=reply,
                model=model_name,
                promptTokens=usage.prompt_tokens if usage else 0,
                completionTokens=usage.completion_tokens if usage else 0,
                rubric=body.rubric,
            ).insert()
        except Exception:
            pass  # logging failures must not break the chat response

    return {
        "reply": reply,
        "model": model_name,
        "promptTokens": usage.prompt_tokens if usage else 0,
        "completionTokens": usage.completion_tokens if usage else 0,
    }


@router.get("/interview/{iv_id}/chat/history")
async def interview_chat_history(
    iv_id: str,
    user: Annotated[UserExtra, Depends(get_current_user)],
    limit: int = Query(100, ge=1, le=500),
) -> dict:
    oid = to_object_id(iv_id)
    iv = await Interview.get(oid)
    if iv is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="interview not found")
    if user.role != "admin" and iv.createdBy != user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")

    query: dict = {"interviewId": oid}
    if user.role != "admin":
        query["userId"] = user.id
    docs = await InterviewChatLog.find(query).sort([("createdAt", 1)]).limit(limit).to_list()
    return {"logs": [doc_to_dict(d) for d in docs]}


@router.delete("/interview/{iv_id}/chat/history")
async def clear_interview_chat_history(
    iv_id: str,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    oid = to_object_id(iv_id)
    iv = await Interview.get(oid)
    if iv is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="interview not found")
    if user.role != "admin" and iv.createdBy != user.id:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")

    query: dict = {"interviewId": oid}
    if user.role != "admin":
        query["userId"] = user.id
    res = await InterviewChatLog.find(query).delete()
    deleted = getattr(res, "deleted_count", 0) or 0
    return {"deleted": deleted}


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
