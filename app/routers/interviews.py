from datetime import datetime
from typing import Annotated, Any, Iterable, Optional

from beanie import PydanticObjectId
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel

from app.deps import get_current_user
from app.models import Account, Interview, UserExtra
from app.routers._util import doc_to_dict, parse_date, populate_owner, to_object_id
from app.slack import notify_new_interview

router = APIRouter(prefix="/interviews", tags=["interviews"])

STAGES = {"intro", "tech1", "tech2", "panel", "hiring_manager", "final", "offer", "others"}


class InterviewCreate(BaseModel):
    accountId: str
    resumeId: str
    scheduledAt: str
    endsAt: str
    stage: str
    jobDescription: Optional[str] = ""
    transcript: Optional[str] = ""
    note: Optional[str] = ""


class InterviewUpdate(BaseModel):
    accountId: Optional[str] = None
    resumeId: Optional[str] = None
    scheduledAt: Optional[str] = None
    endsAt: Optional[str] = None
    stage: Optional[str] = None
    jobDescription: Optional[str] = None
    transcript: Optional[str] = None
    note: Optional[str] = None


def _assert_resume_in_account(account: Account, resume_id: PydanticObjectId) -> None:
    if not any(r.id == resume_id for r in account.resumes):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="resumeId is not in the selected account",
        )


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
    creatorId: str = Query(""),
    sort: str = Query("desc"),
) -> dict:
    # Spec: every authenticated user can see every interview.
    query: dict[str, Any] = {}

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


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_interview(
    body: InterviewCreate,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    if body.stage not in STAGES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid stage")
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

    resume_oid = to_object_id(body.resumeId)
    _assert_resume_in_account(account, resume_oid)

    iv = Interview(
        accountId=account_oid,
        resumeId=resume_oid,
        createdBy=user.id,
        scheduledAt=sched,
        endsAt=ends,
        stage=body.stage,
        jobDescription=body.jobDescription or "",
        transcript=body.transcript or "",
        note=body.note or "",
    )
    await iv.insert()

    try:
        await notify_new_interview(
            creator_email=user.email or "",
            account_email=account.email or "",
            stage=body.stage,
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

    final_account_oid = iv.accountId
    if body.accountId is not None:
        new_account_oid = to_object_id(body.accountId)
        account = await Account.get(new_account_oid)
        if account is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid accountId")
        if not is_admin and str(account.createdBy) != str(user.id):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="cannot use an account you do not own")
        iv.accountId = new_account_oid
        final_account_oid = new_account_oid

    if body.resumeId is not None:
        new_resume_oid = to_object_id(body.resumeId)
        # Re-load the (possibly new) account to validate; reuse `account` if we just loaded it above.
        target_account = account if body.accountId is not None else await Account.get(final_account_oid)
        if target_account is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid accountId")
        _assert_resume_in_account(target_account, new_resume_oid)
        iv.resumeId = new_resume_oid
    elif body.accountId is not None:
        # Account changed but resumeId not patched: existing resumeId may no longer be valid.
        if iv.resumeId is not None:
            target_account = account  # loaded above
            try:
                _assert_resume_in_account(target_account, iv.resumeId)
            except HTTPException:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="resumeId no longer matches new account; pass a new resumeId",
                )
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
        if body.stage not in STAGES:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid stage")
        iv.stage = body.stage
    if body.jobDescription is not None:
        iv.jobDescription = body.jobDescription
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
