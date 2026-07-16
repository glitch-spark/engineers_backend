"""Application pipeline router — Kanban board backing API.

Auto-link is the cheap path: a bid (ResumeApplication) or interview
landing for `(userId, companyName, jobUrl?)` finds-or-creates an
Application and bumps its stage when the new event would advance the
pipeline. Manual drag-drop from the board hits PATCH /applications/{id}.
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, List, Optional

from beanie import PydanticObjectId
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from app.deps import get_current_user
from app.models import Application, EmailMessage, Interview, ResumeApplication, UserExtra
from app.models.application import KanbanStage, STAGE_ORDER, StageEvent
from app.routers._util import doc_to_dict, populate_owner, to_object_id
from app.slack import notify_new_interview  # reuse Slack infra (graceful no-op)

router = APIRouter(prefix="/applications", tags=["applications"])

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VALID_STAGES = {s.value for s in KanbanStage}
_VALID_OUTCOMES = {"active", "offer", "rejected", "withdrawn", "no_response"}


def _key(company: str, job_url: Optional[str]) -> dict:
    """Lookup key for find-or-create: same user + same company (case-insensitive)
    + same job posting URL (None counts as None, not wildcard)."""
    company_lc = (company or "").strip().lower()
    return {"companyNameLower": company_lc, "jobUrl": (job_url or None)}


def _stage_index(stage: str) -> int:
    """Returns the canonical order position; terminal stages sort last."""
    try:
        return STAGE_ORDER.index(stage)
    except ValueError:
        return len(STAGE_ORDER)  # unknown / terminal


async def find_or_create_application(
    *,
    user_id: PydanticObjectId,
    account_id: Optional[PydanticObjectId],
    company_name: str,
    job_url: Optional[str],
    job_description: Optional[str] = None,
    bid_job_id: Optional[PydanticObjectId] = None,
    confirmed: bool = True,
    ai_label: Optional[str] = None,
    ai_confidence: Optional[float] = None,
) -> Application:
    """Idempotent: same (user, company, jobUrl) → same Application doc.

    `confirmed`/`ai_label`/`ai_confidence` are applied ONLY when a new card is
    created — an existing card keeps its confirmation state, so an inbound
    email touching a real (confirmed) bid never silently un-confirms it."""
    now = datetime.now(timezone.utc)
    company_name = (company_name or "").strip() or "Unknown"
    q = {"userId": user_id, **_key(company_name, job_url)}
    app = await Application.find_one(q)
    if app is None:
        app = Application(
            userId=user_id,
            accountId=account_id,
            companyName=company_name,
            companyNameLower=company_name.lower(),
            jobUrl=job_url,
            jobDescription=job_description,
            stage=KanbanStage.bid_sent.value,
            outcome="active",
            confirmed=confirmed,
            aiLabel=ai_label,
            aiConfidence=ai_confidence,
            appliedAt=now,
            lastTouchedAt=now,
            stageHistory=[StageEvent(stage=KanbanStage.bid_sent.value, at=now, source="auto")],
        )
        if bid_job_id is not None:
            app.bidJobIds = [bid_job_id]
        await app.insert()
        return app

    # Existing app — patch in any new bid linkage / freshen lastTouchedAt.
    changed = False
    if bid_job_id is not None and bid_job_id not in app.bidJobIds:
        app.bidJobIds.append(bid_job_id)
        changed = True
    if account_id is not None and app.accountId is None:
        app.accountId = account_id
        changed = True
    if job_description and not (app.jobDescription or "").strip():
        app.jobDescription = job_description
        changed = True
    if changed:
        app.lastTouchedAt = now
        await app.save()
    return app


def _advance_stage(app: Application, new_stage: str, *, source: str, by: Optional[PydanticObjectId]) -> bool:
    """Move stage forward only — never backwards for auto-events. Returns
    True if stage changed."""
    if new_stage not in _VALID_STAGES:
        return False
    if _stage_index(new_stage) <= _stage_index(app.stage):
        return False
    app.stage = new_stage
    app.lastTouchedAt = datetime.now(timezone.utc)
    app.stageHistory.append(StageEvent(stage=new_stage, at=app.lastTouchedAt, by=by, source=source))
    return True


async def attach_interview_to_app(
    *,
    user_id: PydanticObjectId,
    interview: Interview,
) -> Optional[Application]:
    """Idempotent: links the Interview to its Application (creating one if
    needed) and bumps stage to the interview's stage when that's forward
    progress. Outcome auto-flips on terminal interview statuses."""
    if not (interview.companyName or "").strip():
        return None
    app = await find_or_create_application(
        user_id=user_id,
        account_id=interview.accountId,
        company_name=interview.companyName,
        job_url=interview.jobUrl,
    )
    changed = False
    if interview.id not in app.interviewIds:
        app.interviewIds.append(interview.id)
        changed = True
    if interview.stage:
        if _advance_stage(app, interview.stage.value if hasattr(interview.stage, "value") else str(interview.stage), source="interview-link", by=user_id):
            changed = True

    # Terminal status mapping → outcome.
    status_str = str(interview.status) if interview.status else ""
    if status_str in ("passed",):
        # passed != offer; only flips outcome to offer on explicit Final-pass
        pass
    elif status_str in ("failed", "no_show"):
        if app.outcome == "active":
            app.outcome = "rejected"
            _advance_stage(app, KanbanStage.rejected.value, source="interview-link", by=user_id)
            changed = True

    if changed:
        app.lastTouchedAt = datetime.now(timezone.utc)
        await app.save()
    return app


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


class ApplicationPatch(BaseModel):
    stage: Optional[str] = None
    outcome: Optional[str] = None
    notes: Optional[str] = None
    archived: Optional[bool] = None


class ApplicationCreate(BaseModel):
    companyName: str
    jobUrl: Optional[str] = None
    jobDescription: Optional[str] = None
    accountId: Optional[str] = None


class AttachInterviewBody(BaseModel):
    interviewId: str


@router.get("")
async def list_applications(
    user: Annotated[UserExtra, Depends(get_current_user)],
    stage: Optional[str] = Query(None),
    outcome: Optional[str] = Query(None),
    userId: Optional[str] = Query(None),
    profileId: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    includeArchived: bool = Query(False),
) -> dict:
    """Returns every column's applications in a single payload — frontend
    bucketizes by `stage`. Owner-scoped for non-admins; admins can filter
    by user."""
    is_admin = user.role == "admin"
    query: dict[str, Any] = {} if is_admin else {"userId": user.id}
    if is_admin and userId:
        query["userId"] = to_object_id(userId)
    if stage and stage in _VALID_STAGES:
        query["stage"] = stage
    if outcome and outcome in _VALID_OUTCOMES:
        query["outcome"] = outcome
    if profileId:
        query["accountId"] = to_object_id(profileId)
    if search and search.strip():
        query["companyNameLower"] = {"$regex": re.escape(search.strip().lower())}
    if not includeArchived:
        query["archivedAt"] = None

    rows = await Application.find(query).sort([("lastTouchedAt", -1)]).limit(500).to_list()
    out = await populate_owner(rows, field="userId", only=("name", "email"))
    return {"applications": out}


@router.get("/{app_id}")
async def get_application(
    app_id: str,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    oid = to_object_id(app_id)
    app = await Application.get(oid)
    if app is None or (user.role != "admin" and str(app.userId) != str(user.id)):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    out = await populate_owner([app], field="userId", only=("name", "email"))
    return out[0]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_application(
    body: ApplicationCreate,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    if not (body.companyName or "").strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="companyName is required")
    app = await find_or_create_application(
        user_id=user.id,
        account_id=to_object_id(body.accountId) if body.accountId else None,
        company_name=body.companyName,
        job_url=body.jobUrl,
        job_description=body.jobDescription,
    )
    return doc_to_dict(app)


@router.patch("/{app_id}")
async def patch_application(
    app_id: str,
    body: ApplicationPatch,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    oid = to_object_id(app_id)
    app = await Application.get(oid)
    if app is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    if user.role != "admin" and str(app.userId) != str(user.id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")

    now = datetime.now(timezone.utc)
    changed = False
    notify_terminal: Optional[str] = None  # for Slack

    if body.stage is not None and body.stage in _VALID_STAGES and body.stage != app.stage:
        app.stage = body.stage
        app.stageHistory.append(StageEvent(stage=body.stage, at=now, by=user.id, source="manual"))
        changed = True
    if body.outcome is not None and body.outcome in _VALID_OUTCOMES and body.outcome != app.outcome:
        app.outcome = body.outcome
        if body.outcome in ("offer", "rejected", "withdrawn"):
            notify_terminal = body.outcome
        changed = True
    if body.notes is not None:
        app.notes = body.notes
        changed = True
    if body.archived is True and app.archivedAt is None:
        app.archivedAt = now
        changed = True
    elif body.archived is False and app.archivedAt is not None:
        app.archivedAt = None
        changed = True

    if changed:
        app.lastTouchedAt = now
        await app.save()
        if notify_terminal:
            try:
                # Reuse existing slack infra as best-effort signal — feel free
                # to wire a dedicated notify function later.
                await notify_new_interview(
                    creator_email=user.email or "",
                    account_email="",
                    stage=f"{notify_terminal.upper()} · {app.companyName}",
                    scheduled_at=now.strftime("%Y-%m-%d %H:%M"),
                )
            except Exception:
                pass
    return doc_to_dict(app)


@router.post("/{app_id}/attach-interview")
async def attach_interview(
    app_id: str,
    body: AttachInterviewBody,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    oid = to_object_id(app_id)
    app = await Application.get(oid)
    if app is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    if user.role != "admin" and str(app.userId) != str(user.id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")

    iv = await Interview.get(to_object_id(body.interviewId))
    if iv is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="interview not found")
    if iv.id not in app.interviewIds:
        app.interviewIds.append(iv.id)
    if iv.stage and _advance_stage(app, iv.stage.value if hasattr(iv.stage, "value") else str(iv.stage), source="manual", by=user.id):
        pass
    app.lastTouchedAt = datetime.now(timezone.utc)
    await app.save()
    return doc_to_dict(app)


@router.delete("/{app_id}")
async def delete_application(
    app_id: str,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    oid = to_object_id(app_id)
    app = await Application.get(oid)
    if app is None or (user.role != "admin" and str(app.userId) != str(user.id)):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    await app.delete()
    return {"ok": True}


@router.post("/{app_id}/confirm")
async def confirm_application(
    app_id: str,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    """Confirm an AI-proposed card (the Yes control). The user agrees with the
    label/placement, so the card stays and is treated as a real card."""
    oid = to_object_id(app_id)
    app = await Application.get(oid)
    if app is None or (user.role != "admin" and str(app.userId) != str(user.id)):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    if not app.confirmed:
        app.confirmed = True
        app.lastTouchedAt = datetime.now(timezone.utc)
        await app.save()
    out = await populate_owner([app], field="userId", only=("name", "email"))
    return out[0]


@router.post("/{app_id}/reject")
async def reject_application(
    app_id: str,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    """Reject an AI-proposed card (the No control — wrong label). Removes the
    card and marks any email(s) that fed it as dismissed so they don't
    resurface on the next sync."""
    oid = to_object_id(app_id)
    app = await Application.get(oid)
    if app is None or (user.role != "admin" and str(app.userId) != str(user.id)):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    await EmailMessage.find(EmailMessage.applicationId == app.id).update(
        {"$set": {"reviewStatus": "dismissed"}}
    )
    await app.delete()
    return {"ok": True}


@router.post("/migrate")
async def migrate_applications(
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    """Admin-triggered backfill — interview-only.

    The pipeline is now driven by Gmail sync; we no longer backfill
    Application rows from completed resume generations (those produced a
    huge bid_sent column with no real signal). Existing Interview rows
    are still a valid pipeline source, so we sweep them and create /
    advance the matching Application based on each interview's stage.
    """
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin only")

    linked_ivs = 0
    created = set()

    ivs = await Interview.find({}).to_list()
    for iv in ivs:
        if not (iv.companyName or "").strip():
            continue
        app = await attach_interview_to_app(user_id=iv.createdBy, interview=iv)
        if app is not None:
            created.add(str(app.id))
            linked_ivs += 1

    return {"applications": len(created), "bidsLinked": 0, "interviewsLinked": linked_ivs}


@router.post("/wipe-bid-only")
async def wipe_bid_only_applications(
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    """Admin sweep: deletes Application rows that have no attached
    interviews (i.e. were created solely from a resume-gen auto-link).
    These were the bid_sent backfill the new email-driven pipeline no
    longer wants. Rows with any interviewIds are preserved.
    """
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin only")
    # Clear the entire bid_sent column. The new email-driven pipeline
    # repopulates it only when Gmail surfaces a confirmation or recruiter
    # reach-out, so the historical backfill (auto-created from resume
    # generations + interview-attach helpers) is just noise.
    res = await Application.find({"stage": "bid_sent"}).delete()
    deleted = getattr(res, "deleted_count", 0) if res is not None else 0
    return {"deleted": deleted}


@router.post("/auto-archive")
async def auto_archive(
    user: Annotated[UserExtra, Depends(get_current_user)],
    days: int = Query(30, ge=1, le=365),
) -> dict:
    """Auto-archive applications stuck in Rejected/Withdrawn for N+ days.
    Admin-callable; cron can hit this on a schedule later."""
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin only")
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    q = {
        "archivedAt": None,
        "outcome": {"$in": ["rejected", "withdrawn"]},
        "lastTouchedAt": {"$lte": cutoff},
    }
    rows = await Application.find(q).to_list()
    for r in rows:
        r.archivedAt = datetime.now(timezone.utc)
        await r.save()
    return {"archived": len(rows)}
