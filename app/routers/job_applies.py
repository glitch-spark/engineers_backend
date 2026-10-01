"""Job Applies: upload a sheet of job postings, score selected resumes, suggest one resume per profile."""
import json
from datetime import datetime
from typing import Annotated, Any, Optional

from beanie import PydanticObjectId
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from pydantic import BaseModel, Field

from app.deps import get_current_user
from app.job_apply import worker
from app.job_apply.sheet import MAX_BYTES, SheetError, parse_sheet
from app.models import Account, UserExtra
from app.models.job_apply import JobApplyRow, JobApplyRun, RunCounts, RunSelection
from app.routers._util import doc_to_dict, to_object_id

router = APIRouter(prefix="/job-applies", tags=["job-applies"])

ACTIVE = ("queued", "running")
FAILED = ("fetch_failed", "llm_failed")


# ---------- helpers ----------


def _can_see(owner_id: PydanticObjectId, user: UserExtra) -> bool:
    return user.role == "admin" or str(owner_id) == str(user.id)


async def _owned_run(run_id: str, user: UserExtra) -> JobApplyRun:
    run = await JobApplyRun.get(to_object_id(run_id))
    if run is None or not _can_see(run.userId, user):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    return run


async def _owned_row(row_id: str, user: UserExtra) -> JobApplyRow:
    row = await JobApplyRow.get(to_object_id(row_id))
    if row is None or not _can_see(row.userId, user):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    return row


async def _suggested_count(run: JobApplyRun) -> int:
    return await JobApplyRow.find({"runId": run.id, "topScore": {"$gte": run.threshold}}).count()


async def _run_out(run: JobApplyRun, *, with_profiles: bool = False) -> dict:
    out = doc_to_dict(run)
    out["suggested"] = await _suggested_count(run)
    if with_profiles:
        ids = [s.accountId for s in run.selection]
        accounts = {a.id: a for a in await Account.find({"_id": {"$in": ids}}).to_list()}
        out["profiles"] = [
            {
                "accountId": str(i),
                "name": accounts[i].name if i in accounts else "(deleted profile)",
                "country": accounts[i].country if i in accounts else None,
                "region": accounts[i].region if i in accounts else None,
            }
            for i in ids
        ]
    return out


def _suggestion(s) -> dict:
    return {
        "accountId": s.accountId, "resumeId": s.resumeId, "filename": s.filename,
        "total": s.total, "band": s.band, "knockouts": s.knockouts,
    }


def _row_summary(row: JobApplyRow, threshold: int, account_id: str = "") -> dict:
    ex = row.extraction
    return {
        "_id": str(row.id),
        "rowIndex": row.rowIndex,
        "url": row.url,
        "title": row.title,
        "company": row.company,
        "status": row.status,
        "statusReason": row.statusReason,
        "jdSource": row.jdSource,
        "postedDate": row.postedDate.isoformat() if row.postedDate else None,
        "workMode": ex.workMode if ex else None,
        "allowedLocations": [l.model_dump() for l in ex.allowedLocations] if ex else [],
        "timezoneNote": ex.timezoneNote if ex else None,
        "extractionSource": row.extractionSource,
        "gates": [g.model_dump() for g in row.gates],
        "profileGates": [pg.model_dump() for pg in row.profileGates],
        "topScore": row.topScore,
        "suggestions": [
            _suggestion(s) for s in row.bestByProfile
            if s.total >= threshold and (not account_id or s.accountId == account_id)
        ],
        "applied": row.applied,
    }


async def _parse_selection(raw: str, user: UserExtra) -> list[RunSelection]:
    try:
        items = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="selection must be JSON")
    if not isinstance(items, list) or not items:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Select at least one profile.")
    out: list[RunSelection] = []
    resume_count = 0
    for it in items:
        if not isinstance(it, dict):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid selection")
        acc = await Account.get(to_object_id(str(it.get("accountId", ""))))
        if acc is None or not _can_see(acc.createdBy, user):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
        have = {str(r.id) for r in acc.resumes}
        wanted = [str(x) for x in it.get("resumeIds") or []]
        if any(w not in have for w in wanted):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Unknown resume on profile {acc.name}.")
        resume_count += len(wanted) if wanted else len(have)
        out.append(RunSelection(accountId=acc.id, resumeIds=[PydanticObjectId(w) for w in wanted]))
    if resume_count == 0:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="The selected profiles have no resumes.")
    return out


# ---------- runs ----------


@router.post("/runs", status_code=status.HTTP_202_ACCEPTED)
async def create_run(
    user: Annotated[UserExtra, Depends(get_current_user)],
    file: UploadFile = File(...),
    selection: str = Form(...),
    threshold: int = Form(75),
    maxAgeDays: int = Form(30),
) -> dict:
    if await JobApplyRun.find_one({"userId": user.id, "status": {"$in": list(ACTIVE)}}):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A run is already in progress. Wait for it or cancel it.")
    sel = await _parse_selection(selection, user)
    data = await file.read(MAX_BYTES + 1)
    try:
        rows = parse_sheet(file.filename or "", data)
    except SheetError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    if not rows:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No job rows with a URL were found.")

    run = JobApplyRun(
        userId=user.id,
        fileName=(file.filename or "jobs")[:200],
        selection=sel,
        threshold=max(0, min(100, threshold)),
        maxAgeDays=max(1, min(365, maxAgeDays)),
        counts=RunCounts(total=len(rows)),
    )
    await run.insert()
    await JobApplyRow.insert_many([
        JobApplyRow(
            runId=run.id, userId=user.id, rowIndex=r.rowIndex, url=r.url, jdText=r.jdText,
            jdSource="sheet" if r.jdText else None,
        )
        for r in rows
    ])
    if worker.worker_enabled():
        worker.start_run(run.id)
    return {"runId": str(run.id), "status": run.status, "total": len(rows)}


@router.get("/runs")
async def list_runs(user: Annotated[UserExtra, Depends(get_current_user)]) -> dict:
    runs = await JobApplyRun.find({"userId": user.id}).sort("-createdAt").limit(50).to_list()
    return {"runs": [await _run_out(r) for r in runs]}


@router.get("/runs/{run_id}")
async def get_run(run_id: str, user: Annotated[UserExtra, Depends(get_current_user)]) -> dict:
    return await _run_out(await _owned_run(run_id, user), with_profiles=True)


class RunPatch(BaseModel):
    threshold: int = Field(ge=0, le=100)


@router.patch("/runs/{run_id}")
async def update_run(run_id: str, body: RunPatch, user: Annotated[UserExtra, Depends(get_current_user)]) -> dict:
    run = await _owned_run(run_id, user)
    # Targeted $set: a full save() would overwrite counts/status the worker is updating meanwhile.
    await JobApplyRun.get_motor_collection().update_one(
        {"_id": run.id}, {"$set": {"threshold": body.threshold, "updatedAt": datetime.utcnow()}}
    )
    run.threshold = body.threshold
    return await _run_out(run, with_profiles=True)


@router.post("/runs/{run_id}/cancel")
async def cancel_run(run_id: str, user: Annotated[UserExtra, Depends(get_current_user)]) -> dict:
    run = await _owned_run(run_id, user)
    now = datetime.utcnow()
    res = await JobApplyRun.get_motor_collection().update_one(
        {"_id": run.id, "status": {"$in": list(ACTIVE)}},
        {"$set": {"status": "cancelled", "finishedAt": now, "updatedAt": now}},
    )
    if not res.modified_count:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Only a queued or running run can be cancelled.")
    return await _run_out(await JobApplyRun.get(run.id))


@router.post("/runs/{run_id}/retry-failed")
async def retry_failed(run_id: str, user: Annotated[UserExtra, Depends(get_current_user)]) -> dict:
    run = await _owned_run(run_id, user)
    if run.status in ACTIVE:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="The run is still in progress.")
    if await JobApplyRun.find_one({"userId": run.userId, "status": {"$in": list(ACTIVE)}}):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Another run is in progress.")
    coll = JobApplyRow.get_motor_collection()
    res = await coll.update_many(
        {"runId": run.id, "status": {"$in": list(FAILED)}},
        {"$set": {"status": "pending", "statusReason": None, "updatedAt": datetime.utcnow()}},
    )
    reset = res.modified_count
    await JobApplyRun.get_motor_collection().update_one(
        {"_id": run.id},
        {"$set": {"status": "queued", "error": None, "finishedAt": None, "heartbeatAt": None, "updatedAt": datetime.utcnow()},
         "$inc": {"counts.failed": -reset}},
    )
    if worker.worker_enabled():
        worker.start_run(run.id)
    return {"reset": reset}


@router.delete("/runs/{run_id}")
async def delete_run(run_id: str, user: Annotated[UserExtra, Depends(get_current_user)]) -> dict:
    run = await _owned_run(run_id, user)
    if run.status in ACTIVE:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Cancel the run before deleting it.")
    await JobApplyRow.find({"runId": run.id}).delete()
    await run.delete()
    return {"ok": True}


# ---------- rows ----------


@router.get("/runs/{run_id}/rows")
async def list_rows(
    run_id: str,
    user: Annotated[UserExtra, Depends(get_current_user)],
    view: str = Query("all"),
    accountId: str = Query(""),
    minScore: Optional[int] = Query(None),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
) -> dict:
    run = await _owned_run(run_id, user)
    q: dict[str, Any] = {"runId": run.id}
    floor = max(run.threshold, minScore or 0)
    if view == "suggested":
        q["topScore"] = {"$gte": floor}
    elif view == "excluded":
        q["$or"] = [{"status": "excluded"}, {"status": "scored", "topScore": None}]
    elif view == "failed":
        q["status"] = {"$in": list(FAILED)}
    elif minScore is not None:
        q["topScore"] = {"$gte": minScore}
    if accountId:
        q["bestByProfile"] = {"$elemMatch": {"accountId": accountId, **({"total": {"$gte": floor}} if view == "suggested" else {})}}

    total = await JobApplyRow.find(q).count()
    rows = await JobApplyRow.find(q).sort("+rowIndex").skip((page - 1) * limit).limit(limit).to_list()
    pages = (total + limit - 1) // limit
    return {
        "rows": [_row_summary(r, run.threshold, accountId) for r in rows],
        "pagination": {"page": page, "limit": limit, "total": total, "totalPages": pages, "hasNext": page < pages, "hasPrev": page > 1},
    }


@router.get("/rows/{row_id}")
async def get_row(row_id: str, user: Annotated[UserExtra, Depends(get_current_user)]) -> dict:
    row = await _owned_row(row_id, user)
    run = await JobApplyRun.get(row.runId)
    out = _row_summary(row, run.threshold if run else 75)
    out["scores"] = [s.model_dump(mode="json") for s in sorted(row.scores, key=lambda s: s.total, reverse=True)]
    out["extraction"] = row.extraction.model_dump(mode="json") if row.extraction else None
    out["jdText"] = row.jdText
    return out


class RowPatch(BaseModel):
    applied: bool


@router.patch("/rows/{row_id}")
async def update_row(row_id: str, body: RowPatch, user: Annotated[UserExtra, Depends(get_current_user)]) -> dict:
    row = await _owned_row(row_id, user)
    await JobApplyRow.get_motor_collection().update_one(
        {"_id": row.id}, {"$set": {"applied": body.applied, "updatedAt": datetime.utcnow()}}
    )
    return {"_id": str(row.id), "applied": body.applied}
