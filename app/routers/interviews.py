import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Annotated, Any, Iterable, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from app.config import get_settings
from app.deps import get_current_user
from app.interview_analyze import extract_and_score
from app.models import (
    Account,
    AnalyzeChatLog,
    Interview,
    InterviewChatLog,
    InterviewQuestion,
    UserExtra,
)
from app.prompts import FAILURE_MODES, STAGE_DRILL_BANK
from app.models.interview import InterviewStage, InterviewStageEvent, InterviewStatus
from app.routers._util import doc_to_dict, parse_date, populate_owner, to_object_id
from app.slack import notify_new_interview

log = logging.getLogger(__name__)

router = APIRouter(prefix="/interviews", tags=["interviews"])

STAGES = {s.value for s in InterviewStage}
STATUSES = {s.value for s in InterviewStatus}


def _stage_value(stage: Any) -> Optional[str]:
    """Return the canonical stage string. Never use str(enum) — on Python 3.11+
    str(InterviewStage.tech) == 'InterviewStage.tech', which leaked into badges."""
    if stage is None:
        return None
    if isinstance(stage, InterviewStage):
        return stage.value
    if isinstance(stage, Enum):
        return str(getattr(stage, "value", stage))
    s = str(stage).strip()
    if s.startswith("InterviewStage."):
        s = s.split(".", 1)[1]
    return s or None


def _normalize_stage(stage: Optional[str]) -> Optional[str]:
    """Map legacy/generic tech keys to a concrete Tech sub-stage."""
    stage = _stage_value(stage)
    if not stage:
        return stage
    if stage in ("tech", "tech1"):
        return InterviewStage.tech_round_1.value
    if stage == "tech2":
        return InterviewStage.tech_round_2.value
    if stage == "hiring_manager":
        return InterviewStage.cultural.value
    return stage


def _coerce_write_stage(stage: str) -> str:
    """Validate + normalize a stage for create/update writes."""
    normalized = _normalize_stage(stage) or stage
    if normalized not in STAGES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid stage")
    return normalized


def _append_stage_history(
    iv: Interview,
    stage: Optional[str],
    *,
    by: Any,
    source: str,
    previous: Optional[str] = None,
    previous_scheduled_at: Optional[datetime] = None,
) -> None:
    stage = _normalize_stage(stage)
    previous = _normalize_stage(previous)
    if not stage:
        return
    last = iv.stageHistory[-1].stage if iv.stageHistory else None
    last = _normalize_stage(last)
    if last == stage:
        # Keep the tip's interview date in sync with the current scheduledAt.
        if iv.stageHistory and iv.scheduledAt is not None:
            tip = iv.stageHistory[-1]
            if tip.scheduledAt != iv.scheduledAt:
                tip.scheduledAt = iv.scheduledAt
                iv.stageHistory[-1] = tip
        return
    now = datetime.now(timezone.utc)
    sched = iv.scheduledAt
    # Seed history with the prior stage when this is the first recorded move.
    if not iv.stageHistory and previous and previous != stage:
        iv.stageHistory.append(
            InterviewStageEvent(
                stage=previous,
                at=now,
                by=by,
                source="seed",
                scheduledAt=previous_scheduled_at,
            )
        )
    iv.stageHistory.append(
        InterviewStageEvent(
            stage=stage,
            at=now,
            by=by,
            source=source,
            scheduledAt=sched,
        )
    )


class InterviewCreate(BaseModel):
    accountId: str
    scheduledAt: str
    endsAt: str
    stage: Optional[str] = None
    status: Optional[str] = None
    companyName: Optional[str] = None
    interviewerName: Optional[str] = None
    appliedPosition: Optional[str] = None
    jobUrl: Optional[str] = None
    mainTechStack: Optional[str] = None
    transcript: Optional[str] = ""
    note: Optional[str] = ""


class StageHistoryItemIn(BaseModel):
    stage: str
    scheduledAt: Optional[str] = None  # ISO or YYYY-MM-DD


class InterviewUpdate(BaseModel):
    accountId: Optional[str] = None
    scheduledAt: Optional[str] = None
    endsAt: Optional[str] = None
    stage: Optional[str] = None
    status: Optional[str] = None
    companyName: Optional[str] = None
    interviewerName: Optional[str] = None
    appliedPosition: Optional[str] = None
    jobUrl: Optional[str] = None
    mainTechStack: Optional[str] = None
    transcript: Optional[str] = None
    note: Optional[str] = None
    # Ordered stage entries — replaces history so mistaken board moves can be fixed.
    stageHistory: Optional[List[StageHistoryItemIn]] = None


def _parse_datetime(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        if len(s) == 10 and s[4] == "-" and s[7] == "-":
            return datetime.fromisoformat(f"{s}T12:00:00")
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _set_stage_history(
    iv: Interview,
    items: list[StageHistoryItemIn],
    *,
    by: Any,
) -> None:
    """Replace stageHistory from an ordered list (edit-form fixups)."""
    cleaned: list[InterviewStageEvent] = []
    now = datetime.now(timezone.utc)
    for item in items:
        s = _normalize_stage((item.stage or "").strip())
        if not s:
            continue
        if s not in STAGES:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"invalid stage in history: {s}")
        if cleaned and cleaned[-1].stage == s:
            # Prefer a newer scheduledAt when consolidating duplicates.
            sched = _parse_datetime(item.scheduledAt) if item.scheduledAt else None
            if sched is not None:
                cleaned[-1].scheduledAt = sched
            continue
        cleaned.append(
            InterviewStageEvent(
                stage=s,
                at=now,
                by=by,
                source="edit",
                scheduledAt=_parse_datetime(item.scheduledAt) if item.scheduledAt else None,
            )
        )
    iv.stageHistory = cleaned


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
            item[field] = {"_id": str(acc.id), "name": acc.name, "email": ""}
            # Resolve the resume filename for this interview's resumeId, if present.
            resume_id = item.get("resumeId")
            if resume_id:
                match = next((r for r in acc.resumes if str(r.id) == str(resume_id)), None)
                item["resumeFilename"] = match.filename if match else None
            else:
                item["resumeFilename"] = None
    return items


def _stage_filter_values(stage: str) -> list[str]:
    """Expand a list-filter stage into concrete stored stage values."""
    raw = (stage or "").strip()
    if not raw:
        return []
    # Board-level Tech → any tech sub-stage (incl. legacy).
    if raw == "tech":
        return [
            InterviewStage.tech.value,
            InterviewStage.tech_round_1.value,
            InterviewStage.tech_round_2.value,
            InterviewStage.live_coding.value,
            InterviewStage.system_design.value,
            InterviewStage.home_assessment.value,
            "tech1",
            "tech2",
        ]
    normalized = _normalize_stage(raw) or raw
    if normalized not in STAGES and raw not in STAGES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid stage")
    vals = {normalized, raw}
    if normalized == InterviewStage.cultural.value:
        vals.add("hiring_manager")
    if normalized == InterviewStage.tech_round_1.value:
        vals.update({"tech", "tech1"})
    if normalized == InterviewStage.tech_round_2.value:
        vals.add("tech2")
    return list(vals)


@router.get("")
async def list_interviews(
    user: Annotated[UserExtra, Depends(get_current_user)],
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=500),
    from_: str = Query("", alias="from"),
    to: str = Query(""),
    accountId: str = Query(""),
    stage: str = Query(""),
    status_: str = Query("", alias="status"),
    creatorId: str = Query(""),
    sort: str = Query("desc"),
) -> dict:
    # Every authenticated user can list/filter all interviews. Pass creatorId
    # to narrow to one owner; omit it to see everyone.
    query: dict[str, Any] = {}

    date_filter: dict = {}
    f_dt = parse_date(from_)
    t_dt = parse_date(to, end_of_day=True)
    if f_dt:
        date_filter["$gte"] = f_dt
    if t_dt:
        date_filter["$lte"] = t_dt

    stage_vals = _stage_filter_values(stage) if stage else []

    if date_filter and stage_vals:
        # Stage filter = “has this stage badge with a date in range”, not “current pan”.
        query["$or"] = [
            {
                "stageHistory": {
                    "$elemMatch": {
                        "stage": {"$in": stage_vals},
                        "scheduledAt": date_filter,
                    }
                }
            },
            {
                "stage": {"$in": stage_vals},
                "scheduledAt": date_filter,
            },
        ]
    elif date_filter:
        # Include cards whose current schedule OR any stage-round date falls in range.
        query["$or"] = [
            {"scheduledAt": date_filter},
            {"stageHistory.scheduledAt": date_filter},
        ]
    elif stage_vals:
        query["$or"] = [
            {"stage": {"$in": stage_vals}},
            {"stageHistory.stage": {"$in": stage_vals}},
        ]

    if accountId:
        query["accountId"] = to_object_id(accountId)
    if status_:
        if status_ not in STATUSES:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid status")
        query["status"] = status_
    if creatorId:
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
    stage_val = _coerce_write_stage(body.stage) if body.stage else None
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
        stage=InterviewStage(stage_val) if stage_val else None,
        status=InterviewStatus(body.status) if body.status else None,
        companyName=body.companyName or None,
        interviewerName=body.interviewerName or None,
        appliedPosition=body.appliedPosition or None,
        jobUrl=body.jobUrl or None,
        mainTechStack=body.mainTechStack or None,
        transcript=body.transcript or "",
        note=body.note or "",
        stageHistory=[],
    )
    if stage_val:
        _append_stage_history(iv, stage_val, by=user.id, source="create")
    await iv.insert()

    if (iv.transcript or "").strip():
        asyncio.create_task(extract_and_score(iv))

    # Auto-link to pipeline (idempotent + best-effort).
    try:
        from app.routers.applications import attach_interview_to_app
        await attach_interview_to_app(user_id=user.id, interview=iv)
    except Exception as exc:  # noqa: BLE001
        log.warning("pipeline auto-link failed for interview %s: %s", iv.id, exc)

    try:
        await notify_new_interview(
            creator_email=user.email or "",
            account_email="",
            stage=stage_val or "",
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
    prev_sched = iv.scheduledAt
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
    # Edit UI no longer edits times; if endsAt is missing/equal/before start, keep a 1h window.
    if new_sched is not None and (new_ends is None or new_ends <= new_sched):
        new_ends = new_sched + timedelta(hours=1)

    # Before changing the interview date, stamp the current tip with the prior schedule
    # so earlier rounds keep their date when the next round is scheduled.
    if (
        body.scheduledAt is not None
        and body.stageHistory is None
        and iv.stageHistory
        and prev_sched is not None
    ):
        tip = iv.stageHistory[-1]
        if tip.scheduledAt is None:
            tip.scheduledAt = prev_sched
            iv.stageHistory[-1] = tip

    iv.scheduledAt = new_sched
    iv.endsAt = new_ends
    if body.stageHistory is not None:
        _set_stage_history(iv, body.stageHistory, by=user.id)

    if body.stage is not None:
        if body.stage == "":
            iv.stage = None
        else:
            stage_val = _coerce_write_stage(body.stage)
            prev = _stage_value(iv.stage)
            iv.stage = InterviewStage(stage_val)
            if body.stageHistory is None:
                # Board drag / partial update — append movement.
                if stage_val != _normalize_stage(prev):
                    _append_stage_history(
                        iv,
                        stage_val,
                        by=user.id,
                        source="manual",
                        previous=prev,
                        previous_scheduled_at=prev_sched,
                    )
                elif body.scheduledAt is not None:
                    # Same stage, date-only change (e.g. Tech → same Tech sub-stage).
                    _append_stage_history(
                        iv, stage_val, by=user.id, source="manual", previous=prev,
                    )
            else:
                # Edit form sent an explicit history — keep current stage as the tip.
                last = iv.stageHistory[-1].stage if iv.stageHistory else None
                if _normalize_stage(last) != stage_val:
                    _append_stage_history(
                        iv, stage_val, by=user.id, source="edit", previous=last,
                    )
    elif body.stageHistory is not None and iv.stageHistory:
        # History-only edit: current stage follows the last remaining entry.
        tip = _normalize_stage(iv.stageHistory[-1].stage)
        iv.stage = InterviewStage(tip) if tip in STAGES else None
    elif body.stageHistory is not None and not iv.stageHistory:
        iv.stage = None

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
    if body.jobUrl is not None:
        iv.jobUrl = body.jobUrl or None
    if body.mainTechStack is not None:
        iv.mainTechStack = body.mainTechStack or None
    transcript_changed = body.transcript is not None and body.transcript != (iv.transcript or "")
    if body.transcript is not None:
        iv.transcript = body.transcript
    if body.note is not None:
        iv.note = body.note

    await iv.save()

    # Re-sync pipeline (stage bump + outcome flip on status changes).
    try:
        from app.routers.applications import attach_interview_to_app
        await attach_interview_to_app(user_id=user.id, interview=iv)
    except Exception as exc:  # noqa: BLE001
        log.warning("pipeline auto-link failed for interview %s on update: %s", iv.id, exc)

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


from app.prompts import STAGE_LABELS as _STAGE_LABELS


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

    # ---- Pull recent follow-up questions for style/uncertainty signal ----
    chat_logs = (
        await InterviewChatLog.find({"interviewId": {"$in": iv_ids}})
        .sort([("createdAt", -1)])
        .limit(60)
        .to_list()
    )
    follow_up_digest = [(log_.question or "")[:200] for log_ in chat_logs if (log_.question or "").strip()][:30]

    # ---- Filter context block surfaces "what view is the user looking at" ----
    company_set = sorted({iv.companyName for iv in interviews if iv.companyName})
    position_set = sorted({iv.appliedPosition for iv in interviews if iv.appliedPosition})
    filter_context = {
        "stages": list(by_stage.keys()),
        "companies": company_set[:8],
        "positions": position_set[:8],
        "fromDate": body.fromDate,
        "toDate": body.toDate,
        "interviewCount": len(interviews),
        "questionCount": len(questions),
    }

    # ---- Generate per-stage rollup with one LLM call ----
    settings = get_settings()
    if settings.OPENAI_API_KEY:
        client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
        digest = []
        for s in stages_out:
            qs = s["_qsForTips"]
            scored = [q for q in qs if q.score is not None]
            buckets = {"weak": 0, "mixed": 0, "solid": 0, "strong": 0}
            for q in scored:
                if q.score <= 3:
                    buckets["weak"] += 1
                elif q.score <= 6:
                    buckets["mixed"] += 1
                elif q.score <= 8:
                    buckets["solid"] += 1
                else:
                    buckets["strong"] += 1
            samples = [
                {
                    "q": q.question[:200],
                    "a": (q.candidateAnswer or "")[:300],  # answer text required for style read
                    "score": q.score,
                    "rationale": (q.scoreRationale or "")[:200],
                    "tip": (q.improvementTip or "")[:200],
                }
                for q in qs[:10]
            ]
            digest.append({
                "stage": s["stage"],
                "stageLabel": s["stageLabel"],
                "avg": s["overallScore"],
                "questionCount": len(qs),
                "scoreBuckets": buckets,
                "topQuestions": s["topQuestions"],
                "samples": samples,
                "drillBank": STAGE_DRILL_BANK.get(s["stage"], ""),
            })

        rollup_prompt = (
            "You are an interview coach analyzing a candidate's history across MULTIPLE interviews.\n"
            "Goal: surface STATISTICAL patterns (what topics keep coming up, where they shine vs stumble),\n"
            "read their COMMUNICATION STYLE from answer excerpts, and tell them HOW TO SHINE next time.\n\n"
            "Audience IS the candidate. Write as 'you'. Direct, specific, no hedging. No emojis.\n\n"
            "Use the named failure modes when calling out problems. Pick from this fixed list:\n"
            f"{FAILURE_MODES}\n"
            "When picking prep drills, lean on each stage's drillBank but adapt to the candidate's actual gaps.\n\n"
            "Return STRICT JSON only. Schema:\n"
            "{\n"
            '  "stages": [{\n'
            '    "stage": "<stage key from input>",\n'
            '    "askedAlways": ["topics that recur across this stage — keep to 3-5"],\n'
            '    "youShineOn": [{"topic": "...", "evidence": "ref to a sample question/answer"}],\n'
            '    "youStumbleOn": [{"topic": "...", "failureMode": "<one named mode>", "evidence": "..."}],\n'
            '    "communicationStyle": {\n'
            '      "pacing": "<one line>", "structure": "<one line>",\n'
            '      "verbosity": "<one line>", "confidence": "<one line>"\n'
            "    },\n"
            '    "styleImprovements": ["3-5 specific style fixes for this stage"],\n'
            '    "drills": ["1-3 picked from drillBank, adapted to gaps"]\n'
            "  }],\n"
            '  "styleProfile": "1-paragraph signature interview voice (3-4 sentences)",\n'
            '  "signatureStrengths": ["3 patterns to lean into"],\n'
            '  "blindSpots": ["3 patterns hurting signal"],\n'
            '  "howToShine": [{"move": "concrete tactic", "why": "what signal it sends"}],\n'
            '  "interviewTactics": ["3-5 direction-leading tricks: how to steer the interview toward strengths"],\n'
            '  "redFlags": ["recurring patterns the candidate may not realize hurt them"],\n'
            '  "uncertaintyTopics": ["topics the candidate keeps asking AI follow-ups about — likely real gaps"],\n'
            '  "overallTips": ["3 cross-cutting prep tips not tied to one stage"]\n'
            "}\n\n"
            "Rules:\n"
            "- Ground EVERY callout in the digest. No generic advice ('study more', 'be confident').\n"
            "- Quote 5-10 word snippets from samples when citing evidence.\n"
            "- If a section has nothing to say, return an empty array — do NOT fabricate.\n"
            "- Output ONLY the JSON object. No markdown fences, no commentary.\n\n"
            f"Filter context:\n{json.dumps(filter_context, ensure_ascii=False)}\n\n"
            f"Per-stage digest:\n{json.dumps(digest, ensure_ascii=False)}\n\n"
            f"Recent AI follow-up questions the candidate asked (uncertainty signal):\n"
            f"{json.dumps(follow_up_digest, ensure_ascii=False)}"
        )

        try:
            # Style + judgment work — bump to higher-quality model.
            rollup_kwargs: dict = {"model": settings.RESUME_OPENAI_MODEL, "input": rollup_prompt}
            if settings.RESUME_OPENAI_MODEL.startswith("gpt-5"):
                rollup_kwargs["reasoning"] = {"effort": settings.AI_REASONING_EFFORT}
            rollup_res = await client.responses.create(**rollup_kwargs)
            rollup_raw = rollup_res.output_text or ""
            rollup_parsed = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", rollup_raw.strip(), flags=re.MULTILINE))
        except Exception as exc:  # noqa: BLE001
            log.warning("analyze rollup failed: %s", exc)
            rollup_parsed = {}
    else:
        rollup_parsed = {}

    by_stage_out = {s.get("stage"): s for s in (rollup_parsed.get("stages") or []) if isinstance(s, dict)}

    # ---- Final shape ----
    for s in stages_out:
        del s["_qsForTips"]
        t = by_stage_out.get(s["stage"], {})
        s["askedAlways"] = list(t.get("askedAlways") or [])
        s["youShineOn"] = list(t.get("youShineOn") or [])
        s["youStumbleOn"] = list(t.get("youStumbleOn") or [])
        s["communicationStyle"] = dict(t.get("communicationStyle") or {})
        s["styleImprovements"] = list(t.get("styleImprovements") or [])
        s["drills"] = list(t.get("drills") or [])

    return {
        "interviewCount": len(interviews),
        "transcriptCount": transcript_count,
        "questionCount": len(questions),
        "filterContext": filter_context,
        "result": {
            "stages": sorted(stages_out, key=lambda x: -x["interviewCount"]),
            "weakSpots": weak_spots[:8],
            "styleProfile": rollup_parsed.get("styleProfile") or "",
            "signatureStrengths": list(rollup_parsed.get("signatureStrengths") or []),
            "blindSpots": list(rollup_parsed.get("blindSpots") or []),
            "howToShine": list(rollup_parsed.get("howToShine") or []),
            "interviewTactics": list(rollup_parsed.get("interviewTactics") or []),
            "redFlags": list(rollup_parsed.get("redFlags") or []),
            "uncertaintyTopics": list(rollup_parsed.get("uncertaintyTopics") or []),
            "overallTips": list(rollup_parsed.get("overallTips") or []),
        },
    }


# ---------- Analyze chat (follow-up Q&A grounded in a result) ---------------


class AnalyzeChatMessage(BaseModel):
    role: str  # "user" | "assistant"
    content: str


class AnalyzeChatBody(BaseModel):
    accountId: Optional[str] = None
    stages: Optional[List[str]] = None
    fromDate: Optional[str] = None
    toDate: Optional[str] = None
    filterContext: dict = {}
    result: dict  # the AnalyzeResult JSON the user is looking at
    messages: List[AnalyzeChatMessage] = []


_ANALYZE_CHAT_PROMPT = """\
You are an interview coach in a follow-up chat. The user is looking at a
cross-interview ANALYSIS of their own interviews and wants to drill into it.
Audience IS the candidate. Write directly as "you". Direct, specific, no
hedging. No emojis.

You have:
- the filter the user applied (stages / companies / dates),
- the analysis result (stage stats, style profile, blind spots, drills, etc.),
- the conversation so far.

Rules:
- Ground every claim in the analysis. If the user asks about something not in
  the data, say so — don't fabricate.
- Reuse the named failure modes when the user asks about weaknesses:
{failure_modes}
- When asked for prep plans, lean on the per-stage drills already in the result;
  expand them with concrete time-boxed tasks.
- When asked about style, expand on the styleProfile / communicationStyle
  fields. Quote 5-10 word evidence snippets when they help.
- When asked "how do I shine on X", combine howToShine + interviewTactics
  with the specific signal in the result.
- Markdown allowed (headings, bullets, tables).
- Keep responses tight unless the user asks for depth.
"""


@router.post("/analyze/chat")
async def analyze_chat(
    body: AnalyzeChatBody,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    from openai import AsyncOpenAI

    settings = get_settings()
    if not settings.OPENAI_API_KEY:
        raise HTTPException(status_code=500, detail="OPENAI_API_KEY missing")
    if not body.messages:
        raise HTTPException(status_code=400, detail="messages required")

    last_user = next((m for m in reversed(body.messages) if m.role == "user"), None)
    if last_user is None:
        raise HTTPException(status_code=400, detail="no user message in body")

    system_prompt = _ANALYZE_CHAT_PROMPT.format(failure_modes=FAILURE_MODES)
    grounding = (
        "Filter the user is viewing:\n"
        f"{json.dumps(body.filterContext or {}, ensure_ascii=False)}\n\n"
        "Analysis result the user is viewing:\n"
        f"{json.dumps(body.result or {}, ensure_ascii=False)}"
    )

    chat_messages: list[dict] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": grounding},
    ]
    for m in body.messages:
        if m.role not in ("user", "assistant"):
            continue
        chat_messages.append({"role": m.role, "content": m.content})

    client = AsyncOpenAI(api_key=settings.OPENAI_API_KEY)
    chat_kwargs: dict = {
        "model": settings.RESUME_OPENAI_MODEL,
        "messages": chat_messages,
        "max_completion_tokens": settings.AI_CHAT_MAX_TOKENS,
    }
    if settings.RESUME_OPENAI_MODEL.startswith("gpt-5"):
        chat_kwargs["reasoning_effort"] = settings.AI_REASONING_EFFORT
    try:
        completion = await client.chat.completions.create(**chat_kwargs)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(exc))

    choice = completion.choices[0] if completion.choices else None
    reply = (choice.message.content if choice and choice.message else "") or ""
    usage = completion.usage

    # Persist user question for prompt tuning + future suggestion mining.
    try:
        acct_oid = None
        if body.accountId:
            try:
                acct_oid = to_object_id(body.accountId)
            except HTTPException:
                acct_oid = None
        await AnalyzeChatLog(
            userId=user.id,
            accountId=acct_oid,
            filterStages=list(body.stages or []),
            filterFromDate=body.fromDate,
            filterToDate=body.toDate,
            question=last_user.content,
            answer=reply,
            model=settings.RESUME_OPENAI_MODEL,
            promptTokens=usage.prompt_tokens if usage else 0,
            completionTokens=usage.completion_tokens if usage else 0,
        ).insert()
    except Exception:
        pass  # logging never breaks the chat

    return {
        "reply": reply,
        "model": settings.RESUME_OPENAI_MODEL,
        "promptTokens": usage.prompt_tokens if usage else 0,
        "completionTokens": usage.completion_tokens if usage else 0,
    }
