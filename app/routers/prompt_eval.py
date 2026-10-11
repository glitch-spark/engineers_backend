"""Resume prompt evaluator API: the sample JD library and eval runs (`/resume/eval`)."""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Annotated, Any, List, Optional

from beanie import PydanticObjectId
from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from app.deps import get_current_user
from app.models import Account, EvalJobDescription, ResumePromptEval, UserExtra
from app.models.prompt_eval import (
    EVAL_ROLES,
    MAX_JDS_PER_RUN,
    ROLE_ORDER,
    EvalCounts,
    EvalRole,
    EvalRow,
    PromptSource,
)
from app.prompt_eval import runner
from app.routers._util import doc_to_dict, load_owned_account, require_model, to_object_id

router = APIRouter(prefix="/resume/eval", tags=["prompt-eval"])
log = logging.getLogger(__name__)

JD_TEXT_MAX = 40_000


# ---------- job description library ----------


class JdIn(BaseModel):
    role: EvalRole
    title: str = Field(min_length=1, max_length=200)
    company: str = Field(min_length=1, max_length=200)
    text: str = Field(min_length=1, max_length=JD_TEXT_MAX)


class JdPatch(BaseModel):
    role: Optional[EvalRole] = None
    title: Optional[str] = Field(None, min_length=1, max_length=200)
    company: Optional[str] = Field(None, min_length=1, max_length=200)
    text: Optional[str] = Field(None, min_length=1, max_length=JD_TEXT_MAX)


def _jd_sort_key(jd: EvalJobDescription) -> tuple:
    return (ROLE_ORDER.get(jd.role, 99), jd.title.lower())


async def _load_jd(jd_id: str) -> EvalJobDescription:
    jd = await EvalJobDescription.get(to_object_id(jd_id))
    if jd is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    return jd


@router.get("/jds")
async def list_jds(
    user: Annotated[UserExtra, Depends(get_current_user)],
    role: Optional[EvalRole] = None,
    includeArchived: bool = False,
) -> dict:
    """The shared library, in role order then title."""
    query: dict[str, Any] = {}
    if role:
        query["role"] = role
    if not includeArchived:
        query["archived"] = False
    jds = await EvalJobDescription.find(query).to_list()
    jds.sort(key=_jd_sort_key)
    return {"roles": list(EVAL_ROLES), "jds": [doc_to_dict(j) for j in jds]}


@router.post("/jds", status_code=status.HTTP_201_CREATED)
async def create_jd(body: JdIn, user: Annotated[UserExtra, Depends(get_current_user)]) -> dict:
    jd = EvalJobDescription(
        role=body.role, title=body.title.strip(), company=body.company.strip(), text=body.text.strip(),
        createdBy=user.id,
    )
    await jd.insert()
    return doc_to_dict(jd)


@router.put("/jds/{jd_id}")
async def update_jd(jd_id: str, body: JdPatch, user: Annotated[UserExtra, Depends(get_current_user)]) -> dict:
    changes = {k: v for k, v in body.model_dump().items() if v is not None}
    if not changes:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Nothing to update.")
    jd = await _load_jd(jd_id)
    for key, value in changes.items():
        setattr(jd, key, value.strip() if isinstance(value, str) else value)
    await jd.save()
    return doc_to_dict(jd)


@router.post("/jds/{jd_id}/archive")
async def archive_jd(jd_id: str, user: Annotated[UserExtra, Depends(get_current_user)]) -> dict:
    jd = await _load_jd(jd_id)
    if not jd.archived:
        jd.archived = True
        jd.archivedAt = datetime.utcnow()
        await jd.save()
    return doc_to_dict(jd)


@router.post("/jds/{jd_id}/unarchive")
async def unarchive_jd(jd_id: str, user: Annotated[UserExtra, Depends(get_current_user)]) -> dict:
    jd = await _load_jd(jd_id)
    if jd.archived:
        jd.archived = False
        jd.archivedAt = None
        await jd.save()
    return doc_to_dict(jd)


# ---------- runs ----------


class RunIn(BaseModel):
    accountId: str
    # None = use the saved prompt (profile, then your global prompt). A string, even empty, is used as is.
    promptBody: Optional[str] = Field(None, max_length=30_000)
    resumeModel: Optional[str] = None
    judgeModel: Optional[str] = None
    roles: List[EvalRole] = []
    jdIds: List[str] = []
    label: Optional[str] = Field(None, max_length=120)


def resolve_prompt(body_prompt: Optional[str], account: Account, user: UserExtra) -> tuple[PromptSource, str]:
    """Which prompt a run uses: the text in the request, else the profile's, else the user's global one."""
    if body_prompt is not None:
        return "custom", body_prompt.strip()
    profile = (getattr(account, "resumePromptBody", None) or "").strip()
    if profile:
        return "profile", profile
    global_prompt = (getattr(user, "resumePromptBody", None) or "").strip()
    if global_prompt:
        return "global", global_prompt
    return "none", ""


def _check_jd_count(n: int) -> None:
    if n == 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Pick at least one job description.")
    if n > MAX_JDS_PER_RUN:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Pick at most {MAX_JDS_PER_RUN} job descriptions per run.",
        )


async def _select_jds(body: RunIn) -> list[EvalJobDescription]:
    if body.jdIds:
        _check_jd_count(len(body.jdIds))
        ids = [to_object_id(i) for i in body.jdIds]
        jds = await EvalJobDescription.find({"_id": {"$in": ids}}).to_list()
        by_id = {str(j.id): j for j in jds}
        missing = [i for i in body.jdIds if i not in by_id]
        if missing:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Unknown job description id.")
        archived = [j.title for j in jds if j.archived]
        if archived:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Archived job descriptions can't be used: {', '.join(archived[:3])}.",
            )
        jds = [by_id[i] for i in body.jdIds]
    else:
        query: dict[str, Any] = {"archived": False}
        if body.roles:
            query["role"] = {"$in": list(dict.fromkeys(body.roles))}
        jds = await EvalJobDescription.find(query).to_list()
    jds.sort(key=_jd_sort_key)
    _check_jd_count(len(jds))
    return jds


def slim(run_dict: dict) -> dict:
    """List view: rows without the generated resume (text + HTML), which can be large."""
    rows = [{k: v for k, v in row.items() if k not in ("resumeText", "resumeHtml")} for row in run_dict.get("rows", [])]
    return {**run_dict, "rows": rows}


@router.post("/runs", status_code=status.HTTP_202_ACCEPTED)
async def create_run(body: RunIn, user: Annotated[UserExtra, Depends(get_current_user)]) -> dict:
    """Validate everything, insert the run with one pending row per JD, start it in the background."""
    resume_model = require_model("resume", body.resumeModel, user)
    judge_model = require_model("judge", body.judgeModel, user)
    if body.jdIds:
        _check_jd_count(len(body.jdIds))
    account = await load_owned_account(body.accountId, user)
    if not (account.styleTemplate or "").strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No HTML template on this profile. Upload one on the Resume Styles tab first.",
        )
    source, prompt_text = resolve_prompt(body.promptBody, account, user)
    jds = await _select_jds(body)

    run = ResumePromptEval(
        userId=user.id,
        accountId=account.id,
        accountName=account.name,
        label=(body.label or "").strip() or None,
        promptBody=prompt_text,
        promptSource=source,
        resumeModelId=resume_model,
        judgeModelId=judge_model,
        status="running",
        startedAt=datetime.utcnow(),
        rows=[EvalRow(jdId=j.id, role=j.role, jdTitle=j.title, jdCompany=j.company) for j in jds],
        counts=EvalCounts(total=len(jds)),
    )
    await run.insert()
    runner.start_run(run.id)
    log.info("prompt eval %s started: profile=%s jds=%d resume=%s judge=%s", run.id, account.name, len(jds), resume_model, judge_model)
    return {"runId": str(run.id), "status": run.status, "jdCount": len(jds)}


@router.get("/runs")
async def list_runs(
    user: Annotated[UserExtra, Depends(get_current_user)],
    accountId: Optional[str] = None,
    page: int = 1,
    limit: int = 20,
) -> dict:
    query: dict[str, Any] = {} if user.role == "admin" else {"userId": user.id}
    if accountId:
        query["accountId"] = to_object_id(accountId)
    page = max(1, page)
    limit = max(1, min(100, limit))
    total = await ResumePromptEval.find(query).count()
    runs = (
        await ResumePromptEval.find(query)
        .sort("-createdAt")
        .skip((page - 1) * limit)
        .limit(limit)
        .to_list()
    )
    await runner.mark_stale_runs(runs)
    total_pages = (total + limit - 1) // limit if limit else 0
    return {
        "runs": [slim(doc_to_dict(r)) for r in runs],
        "pagination": {
            "page": page,
            "limit": limit,
            "total": total,
            "totalPages": total_pages,
            "hasNext": page < total_pages,
            "hasPrev": page > 1,
        },
    }


async def _load_run(run_id: str, user: UserExtra) -> ResumePromptEval:
    run = await ResumePromptEval.get(to_object_id(run_id))
    if run is None or (user.role != "admin" and str(run.userId) != str(user.id)):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    return run


@router.get("/runs/{run_id}")
async def get_run(run_id: str, user: Annotated[UserExtra, Depends(get_current_user)]) -> dict:
    run = await _load_run(run_id, user)
    await runner.mark_stale_runs([run])
    return doc_to_dict(run)


@router.delete("/runs/{run_id}")
async def delete_run(run_id: str, user: Annotated[UserExtra, Depends(get_current_user)]) -> dict:
    run = await _load_run(run_id, user)
    await run.delete()
    return {"ok": True}
