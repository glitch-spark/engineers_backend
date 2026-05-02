from datetime import datetime
from typing import Annotated, Any, Iterable, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from app.deps import get_current_user
from app.models import Account, Interview, UserExtra
from app.models.interview import InterviewStage, InterviewStatus
from app.routers._util import doc_to_dict, parse_date, populate_owner, to_object_id
from app.slack import notify_new_interview

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
    if body.transcript is not None:
        iv.transcript = body.transcript
    if body.note is not None:
        iv.note = body.note

    await iv.save()
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
    return {"ok": True}
