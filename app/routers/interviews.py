import asyncio
import json
import logging
import re
from datetime import datetime
from typing import Annotated, Any, Iterable, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from app.config import get_settings
from app.deps import get_current_user
from app.interview_analyze import extract_and_score
from app.models import Account, Interview, InterviewQuestion, UserExtra
from app.models.interview import InterviewStage, InterviewStatus
from app.routers._util import doc_to_dict, parse_date, populate_owner, to_object_id
from app.slack import notify_new_interview

log = logging.getLogger(__name__)

router = APIRouter(prefix="/interviews", tags=["interviews"])

STAGES = {s.value for s in InterviewStage}
STATUSES = {s.value for s in InterviewStatus}


class InterviewCreate(BaseModel):
    accountId: str
    scheduledAt: str
    endsAt: str
    stage: Optional[str] = None
    status: Optional[str] = None
    companyName: Optional[str] = None
    interviewerName: Optional[str] = None
    appliedPosition: Optional[str] = None
    mainTechStack: Optional[str] = None
    transcript: Optional[str] = ""
    note: Optional[str] = ""


class InterviewUpdate(BaseModel):
    accountId: Optional[str] = None
    scheduledAt: Optional[str] = None
    endsAt: Optional[str] = None
    stage: Optional[str] = None
    status: Optional[str] = None
    companyName: Optional[str] = None
    interviewerName: Optional[str] = None
    appliedPosition: Optional[str] = None
    mainTechStack: Optional[str] = None
    transcript: Optional[str] = None
    note: Optional[str] = None


def _parse_datetime(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


async def _populate_account(items: Iterable[dict], *, field: str = "accountId") -> list:
    items = list(items)
    account_ids = []
    for item in items:
        raw = item.get(field)
        if raw is None:
            continue
        try:
            account_ids.append(to_object_id(str(raw)))
        except HTTPException:
            continue
    account_ids = list(set(account_ids))
    accounts = await Account.find({"_id": {"$in": account_ids}}).to_list() if account_ids else []
    by_id = {str(a.id): a for a in accounts}

    for item in items:
        acc_id = item.get(field)
        acc = by_id.get(str(acc_id)) if acc_id else None
        if acc is not None:
            item[field] = {"_id": str(acc.id), "name": acc.name, "email": acc.email}
            # Resolve the resume filename for this interview's resumeId, if present.
            resume_id = item.get("resumeId")
            if resume_id:
                match = next((r for r in acc.resumes if str(r.id) == str(resume_id)), None)
                item["resumeFilename"] = match.filename if match else None
            else:
                item["resumeFilename"] = None
    return items


@router.get("")
async def list_interviews(
    user: Annotated[UserExtra, Depends(get_current_user)],
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=100),
    from_: str = Query("", alias="from"),
    to: str = Query(""),
    accountId: str = Query(""),
    stage: str = Query(""),
    status_: str = Query("", alias="status"),
    creatorId: str = Query(""),
    sort: str = Query("desc"),
) -> dict:
    # Non-admins only see their own interviews. Admins see everything and may
    # filter by creator. The creatorId param from a non-admin is ignored.
    is_admin = user.role == "admin"
    query: dict[str, Any] = {} if is_admin else {"createdBy": user.id}

    date_filter: dict = {}
    f_dt = parse_date(from_)
    t_dt = parse_date(to, end_of_day=True)
    if f_dt:
        date_filter["$gte"] = f_dt
    if t_dt:
        date_filter["$lte"] = t_dt
    if date_filter:
        query["scheduledAt"] = date_filter

    if accountId:
        query["accountId"] = to_object_id(accountId)
    if stage:
        if stage not in STAGES:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid stage")
        query["stage"] = stage
    if status_:
        if status_ not in STATUSES:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid status")
        query["status"] = status_
    if creatorId and is_admin:
        query["createdBy"] = to_object_id(creatorId)

    direction = 1 if sort == "asc" else -1
    skip = (page - 1) * limit
    total = await Interview.find(query).count()
    rows = await Interview.find(query).sort([("scheduledAt", direction)]).skip(skip).limit(limit).to_list()
    rows_with_owner = await populate_owner(rows, field="createdBy")
    rows_out = await _populate_account(rows_with_owner)

    total_pages = (total + limit - 1) // limit if limit else 0
    return {
        "interviews": rows_out,
        "pagination": {
            "page": page,
            "limit": limit,
            "total": total,
            "totalPages": total_pages,
            "hasNext": page < total_pages,
            "hasPrev": page > 1,
        },
    }


@router.get("/{iv_id}")
async def get_interview(
    iv_id: str,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    oid = to_object_id(iv_id)
    iv = await Interview.get(oid)
    if iv is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    if user.role != "admin" and str(iv.createdBy) != str(user.id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")
    rows = await populate_owner([iv], field="createdBy")
    out = await _populate_account(rows)
    return out[0]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_interview(
    body: InterviewCreate,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    if body.stage is not None and body.stage not in STAGES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid stage")
    if body.status is not None and body.status not in STATUSES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid status")
    sched = _parse_datetime(body.scheduledAt)
    if sched is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid scheduledAt")
    ends = _parse_datetime(body.endsAt)
    if ends is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid endsAt")
    if ends <= sched:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="endsAt must be after scheduledAt")

    account_oid = to_object_id(body.accountId)
    account = await Account.get(account_oid)
    if account is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid accountId")

    is_admin = user.role == "admin"
    if not is_admin and str(account.createdBy) != str(user.id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="cannot use an account you do not own")

    iv = Interview(
        accountId=account_oid,
        createdBy=user.id,
        scheduledAt=sched,
        endsAt=ends,
        stage=InterviewStage(body.stage) if body.stage else None,
        status=InterviewStatus(body.status) if body.status else None,
        companyName=body.companyName or None,
        interviewerName=body.interviewerName or None,
        appliedPosition=body.appliedPosition or None,
        mainTechStack=body.mainTechStack or None,
        transcript=body.transcript or "",
        note=body.note or "",
    )
    await iv.insert()

    if (iv.transcript or "").strip():
        asyncio.create_task(extract_and_score(iv))

    try:
        await notify_new_interview(
            creator_email=user.email or "",
            account_email=account.email or "",
            stage=body.stage or "",
            scheduled_at=sched.strftime("%Y-%m-%d %H:%M"),
        )
    except Exception:
        pass

    return doc_to_dict(iv)


@router.put("/{iv_id}")
async def update_interview(
    iv_id: str,
    body: InterviewUpdate,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    oid = to_object_id(iv_id)
    iv = await Interview.get(oid)
    if iv is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")

    is_admin = user.role == "admin"
    is_owner = str(iv.createdBy) == str(user.id)
    if not is_admin and not is_owner:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")

    if body.accountId is not None:
        new_account_oid = to_object_id(body.accountId)
        account = await Account.get(new_account_oid)
        if account is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid accountId")
        if not is_admin and str(account.createdBy) != str(user.id):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="cannot use an account you do not own")
        iv.accountId = new_account_oid

    new_sched = iv.scheduledAt
    new_ends = iv.endsAt
    if body.scheduledAt is not None:
        sched = _parse_datetime(body.scheduledAt)
        if sched is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid scheduledAt")
        new_sched = sched
    if body.endsAt is not None:
        ends = _parse_datetime(body.endsAt)
        if ends is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid endsAt")
        new_ends = ends
    if new_ends is not None and new_sched is not None and new_ends <= new_sched:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="endsAt must be after scheduledAt")
    iv.scheduledAt = new_sched
    iv.endsAt = new_ends
    if body.stage is not None:
        if body.stage == "":
            iv.stage = None
        else:
            if body.stage not in STAGES:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid stage")
            iv.stage = InterviewStage(body.stage)
    if body.status is not None:
        if body.status == "":
            iv.status = None
        else:
            if body.status not in STATUSES:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid status")
            iv.status = InterviewStatus(body.status)
    if body.companyName is not None:
        iv.companyName = body.companyName or None
    if body.interviewerName is not None:
        iv.interviewerName = body.interviewerName or None
    if body.appliedPosition is not None:
        iv.appliedPosition = body.appliedPosition or None
    if body.mainTechStack is not None:
        iv.mainTechStack = body.mainTechStack or None
    transcript_changed = body.transcript is not None and body.transcript != (iv.transcript or "")
    if body.transcript is not None:
        iv.transcript = body.transcript
    if body.note is not None:
        iv.note = body.note

    await iv.save()

    if transcript_changed and (iv.transcript or "").strip():
        asyncio.create_task(extract_and_score(iv))

    return doc_to_dict(iv)


@router.delete("/{iv_id}")
async def delete_interview(
    iv_id: str,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    oid = to_object_id(iv_id)
    iv = await Interview.get(oid)
    if iv is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")

    is_admin = user.role == "admin"
    is_owner = str(iv.createdBy) == str(user.id)
    if not is_admin and not is_owner:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")

    await iv.delete()
    await InterviewQuestion.find({"interviewId": iv.id}).delete()
    return {"ok": True}


# ---------- Extracted questions ---------------------------------------------


@router.get("/{iv_id}/questions")
async def list_interview_questions(
    iv_id: str,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    oid = to_object_id(iv_id)
    iv = await Interview.get(oid)
    if iv is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    if user.role != "admin" and str(iv.createdBy) != str(user.id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")
    rows = (
        await InterviewQuestion.find({"interviewId": oid})
        .sort([("createdAt", 1)])
        .to_list()
    )
    return {"questions": [doc_to_dict(q) for q in rows]}


@router.post("/{iv_id}/extract")
async def reextract_interview(
    iv_id: str,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    """Force re-extraction. Useful after fixing transcript or for legacy rows
    that never went through the auto-trigger."""
    oid = to_object_id(iv_id)
    iv = await Interview.get(oid)
    if iv is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    if user.role != "admin" and str(iv.createdBy) != str(user.id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")
    if not (iv.transcript or "").strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No transcript to extract")
    asyncio.create_task(extract_and_score(iv))
    return {"ok": True, "message": "extraction queued"}


# ---------- Analyze (cross-interview LLM rollup) ----------------------------


class AnalyzeBody(BaseModel):
    accountId: Optional[str] = None
    stages: Optional[List[str]] = None
    fromDate: Optional[str] = None
    toDate: Optional[str] = None


_MAX_TRANSCRIPT_CHARS = 15_000  # per interview, capped to keep prompt size bounded
_MAX_INTERVIEWS = 30


_STAGE_LABELS = {
    "intro": "Intro",
    "tech": "Tech",
    "panel": "Panel",
    "live_coding": "Live Coding",
    "system_design": "System Design",
    "cultural": "Cultural",
    "final": "Final",
    "ai_interview": "AI Interview",
}


@router.post("/analyze")
async def analyze_interviews(
    body: AnalyzeBody,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    """Aggregate over pre-extracted InterviewQuestion docs. Mongo does the
    heavy lifting; one small LLM call generates per-stage prep tips."""
    from openai import AsyncOpenAI

    iv_query: dict = {"createdBy": user.id} if user.role != "admin" else {}
    if body.accountId:
        iv_query["accountId"] = to_object_id(body.accountId)
    if body.stages:
        iv_query["stage"] = {"$in": [s for s in body.stages if s in STAGES]}
    sched: dict = {}
    if body.fromDate:
        d = parse_date(body.fromDate)
        if d: sched["$gte"] = d
    if body.toDate:
        d = parse_date(body.toDate)
        if d: sched["$lte"] = d
    if sched:
        iv_query["scheduledAt"] = sched

    interviews = await Interview.find(iv_query).sort([("scheduledAt", -1)]).to_list()
    if not interviews:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No interviews match the filter.",
        )
    iv_ids = [iv.id for iv in interviews]

    questions = (
        await InterviewQuestion.find({"interviewId": {"$in": iv_ids}})
        .to_list()
    )
    transcript_count = len({q.interviewId for q in questions})

    if not questions:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "No extracted questions yet for the selected interviews. "
                "Add transcripts and let extraction finish, or use the re-extract action."
            ),
        )

    # ---- Aggregate per stage ----
    by_stage: dict[str, list[InterviewQuestion]] = {}
    for q in questions:
        key = q.stage or "unknown"
        by_stage.setdefault(key, []).append(q)

    stages_out: List[dict] = []
    weak_spots: List[dict] = []

    for stage_key, qs in by_stage.items():
        scored = [q for q in qs if q.score is not None]
        avg = (sum(q.score for q in scored) / len(scored)) if scored else 0.0  # type: ignore[misc]
        # frequency by lowercased question prefix (string-match v2; semantic in v3)
        freq: dict[str, list[InterviewQuestion]] = {}
        for q in qs:
            key = q.question.strip().lower()[:120]
            freq.setdefault(key, []).append(q)
        top = sorted(freq.values(), key=lambda lst: -len(lst))[:5]
        top_questions = [
            {
                "question": grp[0].question,
                "frequency": len(grp),
                "exampleScore": grp[0].score if grp[0].score is not None else 0,
            }
            for grp in top
        ]
        # weak: bottom 3 by score
        bottom = sorted(scored, key=lambda q: q.score)[:3]  # type: ignore[arg-type]
        for q in bottom:
            if q.score is None or q.score >= 6:
                continue
            weak_spots.append({
                "topic": q.question[:80],
                "stage": stage_key,
                "explanation": q.scoreRationale or "Low score on this question.",
                "tip": q.improvementTip or "Practice this topic.",
            })

        stages_out.append({
            "stage": stage_key,
            "stageLabel": _STAGE_LABELS.get(stage_key, stage_key.title()),
            "interviewCount": len({q.interviewId for q in qs}),
            "overallScore": round(avg, 1),
            "topQuestions": top_questions,
            "_qsForTips": qs,  # private, removed before return
        })

    # ---- Generate per-stage tips with one small LLM call ----
    settings = get_settings()
    if settings.OPENAI_API_KEY:
        client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
        digest = []
        for s in stages_out:
            qs = s["_qsForTips"]
            sample = [
                {
                    "q": q.question[:200],
                    "score": q.score,
                    "rationale": (q.scoreRationale or "")[:200],
                    "tip": (q.improvementTip or "")[:200],
                }
                for q in qs[:8]
            ]
            digest.append({
                "stage": s["stage"],
                "avg": s["overallScore"],
                "samples": sample,
            })
        tips_prompt = (
            "Given a candidate's per-stage interview question samples, return JSON with "
            "strengths, weaknesses, and prep tips per stage, plus 3 overall tips.\n\n"
            'Schema: {"stages": [{"stage": "<key>", "strengths": ["..."], "weaknesses": ["..."], "tips": ["..."]}], "overallTips": ["..."]}\n\n'
            "Tips must be specific and actionable, ground in the samples. Output ONLY the JSON.\n\n"
            f"Data:\n{json.dumps(digest, ensure_ascii=False)}"
        )
        try:
            # Tips digest is small + low-stakes; cheaper model is fine.
            tips_res = await client.responses.create(
                model=settings.EXTRACTION_MODEL,
                input=tips_prompt,
            )
            tips_raw = tips_res.output_text or ""
            tips_parsed = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", tips_raw.strip(), flags=re.MULTILINE))
        except Exception as exc:  # noqa: BLE001
            log.warning("tips generation failed: %s", exc)
            tips_parsed = {"stages": [], "overallTips": []}
    else:
        tips_parsed = {"stages": [], "overallTips": []}

    tips_by_stage = {s.get("stage"): s for s in (tips_parsed.get("stages") or []) if isinstance(s, dict)}

    # ---- Final shape ----
    for s in stages_out:
        del s["_qsForTips"]
        t = tips_by_stage.get(s["stage"], {})
        s["strengths"] = list(t.get("strengths") or [])
        s["weaknesses"] = list(t.get("weaknesses") or [])
        s["tips"] = list(t.get("tips") or [])

    return {
        "interviewCount": len(interviews),
        "transcriptCount": transcript_count,
        "questionCount": len(questions),
        "result": {
            "stages": sorted(stages_out, key=lambda x: -x["interviewCount"]),
            "weakSpots": weak_spots[:8],
            "overallTips": list(tips_parsed.get("overallTips") or []),
        },
    }
