"""Job Applies: upload a sheet of job postings, score selected resumes, suggest one resume per profile."""
import asyncio
import json
from datetime import date, datetime, timezone
from typing import Annotated, Any, Optional

import httpx
from beanie import PydanticObjectId
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from pydantic import BaseModel, Field

from app.deps import get_current_user
from app import resume_queue
from app.job_apply import gsheet, worker
from app.job_apply.resume_parse import health
from app.job_apply.sheet import MAX_BYTES, SheetError, parse_sheet
from app.models import Account, ResumeApplication, UserExtra
from app.job_apply.schema import GateResult, Location
from app.models.job_apply import (
    AppliedMark,
    file_key,
    JobApplicationLog,
    JobApplyRow,
    JobApplyRun,
    ProfileGates,
    RunCounts,
    RunSelection,
)
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
    return await JobApplyRow.find(
        {"runId": run.id, "$or": [{"topScore": {"$gte": run.threshold}}, {"tailorOnly.0": {"$exists": True}}]}
    ).count()


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
        # Parseability is a per-resume health badge, not part of the score (spec §3.1).
        out["resumes"] = []
        for sel in run.selection:
            acc = accounts.get(sel.accountId)
            wanted = {str(x) for x in sel.resumeIds}
            for r in acc.resumes if acc else []:
                if wanted and str(r.id) not in wanted:
                    continue
                h = health(r.markdown)
                out["resumes"].append({
                    "accountId": str(acc.id), "resumeId": str(r.id), "filename": r.filename,
                    "health": {"score": h.score, "issues": h.issues}, "hasFile": bool(r.s3Key),
                })
    return out


class _SuggestionView(BaseModel):
    accountId: str
    resumeId: str
    filename: str
    total: int
    band: str
    knockouts: list[str] = Field(default_factory=list)


class _ExtractionView(BaseModel):
    workMode: str = "unknown"
    allowedLocations: list[Location] = Field(default_factory=list)
    timezoneNote: Optional[str] = None


class _ScoreBrief(BaseModel):
    accountId: str
    resumeId: str
    filename: str
    total: int


class _RowListView(BaseModel):
    id: PydanticObjectId = Field(alias="_id")
    rowIndex: int
    url: Optional[str] = None
    title: str = ""
    company: str = ""
    status: str
    statusReason: Optional[str] = None
    jdSource: Optional[str] = None
    postedDate: Optional[date] = None
    extraction: Optional[_ExtractionView] = None
    extractionSource: Optional[str] = None
    gates: list[GateResult] = Field(default_factory=list)
    profileGates: list[ProfileGates] = Field(default_factory=list)
    bestByProfile: list[_SuggestionView] = Field(default_factory=list)
    topScore: Optional[int] = None
    applied: bool = False
    appliedResumes: list[AppliedMark] = Field(default_factory=list)
    scores: list[_ScoreBrief] = Field(default_factory=list)
    tailoredJobId: Optional[PydanticObjectId] = None
    tailoredAccountId: Optional[str] = None
    tailorOnly: list[str] = Field(default_factory=list)

    class Settings:
        projection = {
            "_id": 1, "rowIndex": 1, "url": 1, "title": 1, "company": 1, "status": 1, "statusReason": 1,
            "jdSource": 1, "postedDate": 1, "extractionSource": 1, "gates": 1, "profileGates": 1, "topScore": 1,
            "applied": 1, "appliedResumes": 1, "tailoredJobId": 1, "tailoredAccountId": 1, "tailorOnly": 1,
            "scores.accountId": 1, "scores.resumeId": 1, "scores.filename": 1, "scores.total": 1,
            "extraction.workMode": 1, "extraction.allowedLocations": 1, "extraction.timezoneNote": 1,
            "bestByProfile.accountId": 1, "bestByProfile.resumeId": 1, "bestByProfile.filename": 1,
            "bestByProfile.total": 1, "bestByProfile.band": 1, "bestByProfile.knockouts": 1,
        }


def _suggestion(s) -> dict:
    return {
        "accountId": s.accountId, "resumeId": s.resumeId, "filename": s.filename,
        "total": s.total, "band": s.band, "knockouts": s.knockouts,
    }


def _applied_marks(row, suggestions: list[dict]) -> list[dict]:
    marks = [m.model_dump(mode="json") for m in row.appliedResumes]
    if not marks and row.applied and suggestions:  # legacy single tick = the top suggestion
        top = suggestions[0]
        marks = [{"accountId": top["accountId"], "resumeId": top["resumeId"], "at": None}]
    return marks


_TAILORED_PROJECTION = {"pdfData": 0, "jobDescription": 0, "plainText": 0, "coverLetterText": 0, "screeningPairs": 0}


def _tailored_out(doc: Optional[dict]) -> Optional[dict]:
    if not doc:
        return None
    return {
        "jobId": str(doc["_id"]),
        "accountId": str(doc.get("accountId") or ""),
        "status": doc.get("status"),
        "step": doc.get("step"),
        # A completed generation always has its PDF (in S3 or inline).
        "hasPdf": doc.get("status") == "completed",
        "error": doc.get("errorMessage"),
    }


async def _tailored_docs(ids: list) -> dict[str, dict]:
    ids = [i for i in ids if i]
    if not ids:
        return {}
    cursor = ResumeApplication.get_motor_collection().find({"_id": {"$in": ids}}, _TAILORED_PROJECTION)
    return {str(d["_id"]): d async for d in cursor}


def _row_summary(
    row: JobApplyRow,
    threshold: int,
    account_id: str = "",
    previous: Optional[list] = None,
    tailored: Optional[dict] = None,
) -> dict:
    ex = row.extraction
    suggestions = [
        _suggestion(s) for s in row.bestByProfile
        if s.total >= threshold and (not account_id or s.accountId == account_id)
    ]
    marks = _applied_marks(row, suggestions)
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
        "suggestions": suggestions,
        "appliedResumes": marks,
        "applied": bool(marks),
        # Applications to this URL recorded in other runs (permanent applied log).
        "previouslyApplied": previous or [],
        "tailored": _tailored_out(tailored),
        # Profiles without uploaded resumes this job is open to (apply with a tailored resume).
        "tailorOnly": row.tailorOnly,
        # Every other scored resume (below the threshold or filtered out), for "Other resume…".
        "otherResumes": sorted(
            (
                {"accountId": s.accountId, "resumeId": s.resumeId, "filename": s.filename, "total": s.total}
                for s in row.scores
                if s.resumeId not in {x["resumeId"] for x in suggestions}
            ),
            key=lambda o: o["total"],
            reverse=True,
        ),
    }


async def _parse_selection(raw: str, user: UserExtra) -> list[RunSelection]:
    try:
        items = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="selection must be JSON")
    if not isinstance(items, list) or not items:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Select at least one profile.")
    out: list[RunSelection] = []
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
        if not have and not (acc.styleTemplate or "").strip():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"{acc.name} has no resumes and no HTML template, so it can't be matched or tailored.",
            )
        out.append(RunSelection(accountId=acc.id, resumeIds=[PydanticObjectId(w) for w in wanted]))
    return out


async def _rows_query(
    run: JobApplyRun, *, view: str = "all", account_id: str = "", min_score: Optional[int] = None, applied: str = "any"
) -> dict[str, Any]:
    """The rows filter behind the table, the 'To apply' count and 'Mark all'. 'Applied' means marked in this
    run, or the URL is in the permanent log from another run."""
    q: dict[str, Any] = {"runId": run.id}
    floor = max(run.threshold, min_score or 0)
    if view == "suggested":
        q["$or"] = [{"topScore": {"$gte": floor}}, {"tailorOnly.0": {"$exists": True}}]
    elif view == "excluded":
        q["$or"] = [{"status": "excluded"}, {"status": "scored", "topScore": None}]
    elif view == "failed":
        q["status"] = {"$in": list(FAILED)}
    elif min_score is not None:
        q["topScore"] = {"$gte": min_score}
    if account_id:
        by_profile = {"$or": [
            {"bestByProfile": {"$elemMatch": {"accountId": account_id, **({"total": {"$gte": floor}} if view == "suggested" else {})}}},
            {"tailorOnly": account_id},
        ]}
        q = {"$and": [q, by_profile]}
    if applied != "any":
        run_urls = await JobApplyRow.get_motor_collection().distinct("url", {"runId": run.id})
        logged = await JobApplicationLog.get_motor_collection().distinct(
            "url", {"userId": run.userId, "url": {"$in": run_urls}, "runId": {"$ne": run.id}}
        )
        cond = (
            {"$or": [{"appliedResumes.0": {"$exists": True}}, {"applied": True}, {"url": {"$in": logged}}]}
            if applied == "yes"
            else {"appliedResumes.0": {"$exists": False}, "applied": {"$ne": True}, "url": {"$nin": logged}}
        )
        q = {"$and": [q, cond]}
    return q


# ---------- runs ----------


async def _read_sheet(file: Optional[UploadFile], sheet_url: Optional[str]) -> tuple[str, list]:
    """The job sheet from an upload or a link-shared Google Sheet; 400 with a user-facing message on problems."""
    try:
        if sheet_url:
            async with httpx.AsyncClient() as http:
                return await gsheet.download_gsheet(sheet_url, client=http)
        if file is not None:
            data = await file.read(MAX_BYTES + 1)
            return file.filename or "jobs", await asyncio.to_thread(parse_sheet, file.filename or "", data)
        raise SheetError("Upload a sheet or paste a Google Sheets link.")
    except SheetError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


@router.post("/sheet-preview")
async def sheet_preview(
    user: Annotated[UserExtra, Depends(get_current_user)],
    file: Optional[UploadFile] = File(None),
    sheetUrl: str = Form(""),
) -> dict:
    """Read a job sheet without starting a run, so problems (not shared, no URL column, empty) show up early."""
    title, rows = await _read_sheet(file, sheetUrl.strip() or None)
    with_jd = sum(1 for r in rows if r.jdText)
    return {"title": title, "total": len(rows), "withDescription": with_jd, "urlOnly": len(rows) - with_jd}


@router.post("/runs", status_code=status.HTTP_202_ACCEPTED)
async def create_run(
    user: Annotated[UserExtra, Depends(get_current_user)],
    selection: str = Form(...),
    file: Optional[UploadFile] = File(None),
    sheetUrl: str = Form(""),
    threshold: int = Form(75),
    maxAgeDays: int = Form(30),
) -> dict:
    """The job sheet comes either as an uploaded file or as a link-shared Google Sheet (`sheetUrl`)."""
    if await JobApplyRun.find_one({"userId": user.id, "status": {"$in": list(ACTIVE)}}):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A run is already in progress. Wait for it or cancel it.")
    sel = await _parse_selection(selection, user)
    source_url = sheetUrl.strip() or None
    file_name, rows = await _read_sheet(file, source_url)
    if not rows:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No job rows with a URL were found.")

    run = JobApplyRun(
        userId=user.id,
        fileName=file_name[:200],
        sourceUrl=source_url,
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


def _naive_utc(value: str) -> datetime:
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="appliedSince must be an ISO date-time")
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


@router.get("/runs/{run_id}")
async def get_run(
    run_id: str,
    user: Annotated[UserExtra, Depends(get_current_user)],
    appliedSince: Optional[str] = Query(None, description="ISO date-time (the viewer's local midnight) for 'applied today'"),
) -> dict:
    run = await _owned_run(run_id, user)
    out = await _run_out(run, with_profiles=True)
    log = JobApplicationLog.get_motor_collection()
    out["appliedInRun"] = await log.count_documents({"runId": run.id})
    out["toApply"] = await JobApplyRow.find(await _rows_query(run, view="suggested", applied="no")).count()
    tailored_ids = [
        i for i in await JobApplyRow.get_motor_collection().distinct("tailoredJobId", {"runId": run.id}) if i
    ]
    jobs = ResumeApplication.get_motor_collection()
    by_status = {
        s: await jobs.count_documents({"_id": {"$in": tailored_ids}, "status": s})
        for s in ("queued", "in_progress", "completed", "failed")
    } if tailored_ids else dict.fromkeys(("queued", "in_progress", "completed", "failed"), 0)
    out["tailoring"] = {
        "queued": by_status["queued"], "inProgress": by_status["in_progress"],
        "ready": by_status["completed"], "failed": by_status["failed"],
    }
    if appliedSince:
        out["appliedSince"] = await log.count_documents({"userId": run.userId, "appliedAt": {"$gte": _naive_utc(appliedSince)}})
    return out


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
    # Tailored resumes still waiting in the queue would cost money for a run that's gone; finished ones stay
    # on the Generated page.
    tailored_ids = [i for i in await JobApplyRow.get_motor_collection().distinct("tailoredJobId", {"runId": run.id}) if i]
    if tailored_ids:
        await ResumeApplication.get_motor_collection().delete_many({"_id": {"$in": tailored_ids}, "status": "queued"})
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
    applied: str = Query("any", pattern="^(any|yes|no)$"),
    page: int = Query(1, ge=1),
    limit: int = Query(50, ge=1, le=200),
) -> dict:
    run = await _owned_run(run_id, user)
    q = await _rows_query(run, view=view, account_id=accountId, min_score=minScore, applied=applied)

    total = await JobApplyRow.find(q).count()
    rows = await (
        JobApplyRow.find(q).sort("+rowIndex").skip((page - 1) * limit).limit(limit).project(_RowListView).to_list()
    )
    pages = (total + limit - 1) // limit
    previous: dict[str, list] = {}
    page_urls = [r.url for r in rows if r.url]
    if page_urls:
        cursor = JobApplicationLog.get_motor_collection().find(
            {"userId": run.userId, "url": {"$in": page_urls}, "runId": {"$ne": run.id}}
        ).sort("appliedAt", -1)
        async for entry in cursor:
            previous.setdefault(entry["url"], []).append({
                "appliedAt": entry["appliedAt"].isoformat() if entry.get("appliedAt") else None,
                "profileName": entry.get("profileName", ""),
                "filename": entry.get("filename", ""),
                "accountId": entry.get("accountId"),
            })
    tailored = await _tailored_docs([r.tailoredJobId for r in rows])
    return {
        "rows": [
            _row_summary(r, run.threshold, accountId, previous.get(r.url), tailored.get(str(r.tailoredJobId)))
            for r in rows
        ],
        "pagination": {"page": page, "limit": limit, "total": total, "totalPages": pages, "hasNext": page < pages, "hasPrev": page > 1},
    }


@router.get("/rows/{row_id}")
async def get_row(row_id: str, user: Annotated[UserExtra, Depends(get_current_user)]) -> dict:
    row = await _owned_row(row_id, user)
    run = await JobApplyRun.get(row.runId)
    tailored = await _tailored_docs([row.tailoredJobId])
    out = _row_summary(row, run.threshold if run else 75, tailored=tailored.get(str(row.tailoredJobId)))
    out["scores"] = [s.model_dump(mode="json") for s in sorted(row.scores, key=lambda s: s.total, reverse=True)]
    out["extraction"] = row.extraction.model_dump(mode="json") if row.extraction else None
    out["jdText"] = row.jdText
    return out


# ---------- applied marks ----------


async def _set_applied(
    row: JobApplyRow,
    account_id: str,
    filename: str,
    applied: bool,
    *,
    resume_id: Optional[str] = None,
    tailored_job_id: Optional[str] = None,
) -> None:
    """Mark/unmark one file (uploaded resume or the tailored one) on one job: the row's appliedResumes (this
    run's table), the permanent applied log (later runs), and, for a tailored resume, whether it counts as a bid.
    Idempotent both ways."""
    now = datetime.utcnow()
    rows = JobApplyRow.get_motor_collection()
    log = JobApplicationLog.get_motor_collection()
    fkey = file_key(resume_id, tailored_job_id)
    key = {"userId": row.userId, "url": row.url, "fileKey": fkey}
    field, value = ("tailoredJobId", tailored_job_id) if tailored_job_id else ("resumeId", resume_id)
    if applied:
        await rows.update_one(
            {"_id": row.id, f"appliedResumes.{field}": {"$ne": value}},
            {"$push": {"appliedResumes": {"accountId": account_id, "resumeId": resume_id,
                                          "tailoredJobId": tailored_job_id, "at": now}},
             "$set": {"applied": False, "updatedAt": now}},
        )
        acc = await Account.get(PydanticObjectId(account_id)) if PydanticObjectId.is_valid(account_id) else None
        await log.update_one(
            key,
            {"$set": {**key, "company": row.company, "title": row.title, "accountId": account_id,
                      "profileName": acc.name if acc else "", "resumeId": resume_id, "tailoredJobId": tailored_job_id,
                      "filename": filename, "runId": row.runId, "rowId": row.id, "updatedAt": now},
             "$setOnInsert": {"appliedAt": now, "createdAt": now}},
            upsert=True,
        )
    else:
        await rows.update_one(
            {"_id": row.id},
            {"$pull": {"appliedResumes": {field: value}}, "$set": {"applied": False, "updatedAt": now}},
        )
        await log.delete_one(key)
    if tailored_job_id and PydanticObjectId.is_valid(tailored_job_id):
        # A tailored resume becomes a bid when it's actually used to apply (spec §4).
        await ResumeApplication.get_motor_collection().update_one(
            {"_id": PydanticObjectId(tailored_job_id)}, {"$set": {"countsAsBid": bool(applied), "updatedAt": now}}
        )


class AppliedBody(BaseModel):
    accountId: str
    resumeId: Optional[str] = None  # an uploaded resume scored for this job
    tailoredJobId: Optional[str] = None  # or this job's tailored resume
    applied: bool


@router.put("/rows/{row_id}/applied")
async def set_row_applied(row_id: str, body: AppliedBody, user: Annotated[UserExtra, Depends(get_current_user)]) -> dict:
    row = await _owned_row(row_id, user)
    if bool(body.resumeId) == bool(body.tailoredJobId):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Give either resumeId or tailoredJobId.")
    if not row.url:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="This job has no URL.")
    if body.tailoredJobId:
        job = await ResumeApplication.get_motor_collection().find_one(
            {"_id": to_object_id(body.tailoredJobId)}, {"status": 1, "pdfFilename": 1, "accountId": 1}
        )
        if job is None or str(row.tailoredJobId) != body.tailoredJobId:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="That isn't this job's tailored resume.")
        if body.applied and job.get("status") != "completed":
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="The tailored resume isn't ready yet.")
        filename = job.get("pdfFilename") or "Tailored resume"
        await _set_applied(row, str(job.get("accountId") or body.accountId), filename, body.applied,
                           tailored_job_id=body.tailoredJobId)
    else:
        score = next((s for s in row.scores if s.accountId == body.accountId and s.resumeId == body.resumeId), None)
        if score is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="That resume wasn't scored for this job.")
        await _set_applied(row, body.accountId, score.filename, body.applied, resume_id=body.resumeId)
    fresh = await JobApplyRow.get(row.id)
    return {"_id": str(row.id), "appliedResumes": [m.model_dump(mode="json") for m in fresh.appliedResumes]}


class BulkAppliedBody(BaseModel):
    rowIds: list[str] = Field(default_factory=list, max_length=1000)
    # True = every job still to apply to in this run (all pages), instead of rowIds.
    all: bool = False
    # With a profile filter, use that profile's best resume.
    accountId: str = ""


@router.post("/runs/{run_id}/mark-top-applied")
async def mark_top_applied(
    run_id: str, body: BulkAppliedBody, user: Annotated[UserExtra, Depends(get_current_user)]
) -> dict:
    """Mark the top suggestion applied for the selected jobs, or for every job still to apply to. Skips jobs
    with no suggestion, already marked, or applied in an earlier run. Returns the marks made, for Undo."""
    run = await _owned_run(run_id, user)
    if body.all:
        q = await _rows_query(run, view="suggested", account_id=body.accountId, applied="no")
    else:
        q = {"_id": {"$in": [to_object_id(i) for i in body.rowIds]}, "runId": run.id}
        q = {"$and": [q, await _rows_query(run, applied="no")]}
    marks = []
    rows = await JobApplyRow.find(q).to_list()
    tailored = await _tailored_docs([r.tailoredJobId for r in rows])
    for row in rows:
        if row.appliedResumes or row.applied:
            continue
        ready = tailored.get(str(row.tailoredJobId))
        if ready and ready.get("status") == "completed" and (not body.accountId or row.tailoredAccountId == body.accountId):
            tid = str(row.tailoredJobId)
            await _set_applied(row, row.tailoredAccountId or "", ready.get("pdfFilename") or "Tailored resume", True,
                               tailored_job_id=tid)
            marks.append({"rowId": str(row.id), "accountId": row.tailoredAccountId or "", "tailoredJobId": tid})
            continue
        tops = [
            s for s in row.bestByProfile
            if s.total >= run.threshold and (not body.accountId or s.accountId == body.accountId)
        ]
        if not tops:
            continue
        await _set_applied(row, tops[0].accountId, tops[0].filename, True, resume_id=tops[0].resumeId)
        marks.append({"rowId": str(row.id), "accountId": tops[0].accountId, "resumeId": tops[0].resumeId})
    return {"marked": len(marks), "marks": marks}


class MarkRef(BaseModel):
    rowId: str
    accountId: str
    resumeId: Optional[str] = None
    tailoredJobId: Optional[str] = None


class UnmarkBody(BaseModel):
    marks: list[MarkRef] = Field(max_length=1000)


@router.post("/runs/{run_id}/unmark-applied")
async def unmark_applied(run_id: str, body: UnmarkBody, user: Annotated[UserExtra, Depends(get_current_user)]) -> dict:
    """Undo for a bulk mark: removes exactly the given marks."""
    run = await _owned_run(run_id, user)
    rows = {
        str(r.id): r
        for r in await JobApplyRow.find({"_id": {"$in": [to_object_id(m.rowId) for m in body.marks]}, "runId": run.id}).to_list()
    }
    done = 0
    for m in body.marks:
        row = rows.get(m.rowId)
        if not row:
            continue
        if m.tailoredJobId and any(a.tailoredJobId == m.tailoredJobId for a in row.appliedResumes):
            await _set_applied(row, m.accountId, "", False, tailored_job_id=m.tailoredJobId)
            done += 1
        elif m.resumeId and any(a.resumeId == m.resumeId for a in row.appliedResumes):
            await _set_applied(row, m.accountId, "", False, resume_id=m.resumeId)
            done += 1
    return {"unmarked": done}


# ---------- tailored resumes ----------


def _host(url: Optional[str]) -> str:
    from urllib.parse import urlsplit

    return (urlsplit(url or "").hostname or "").removeprefix("www.")


def _best_profile(row: JobApplyRow, run: JobApplyRun) -> str:
    """The profile whose resume scored best on this job (even below the threshold); else the run's first."""
    if row.scores:
        return max(row.scores, key=lambda s: s.total).accountId
    if row.tailorOnly:
        return row.tailorOnly[0]
    return str(run.selection[0].accountId)


async def _tailor_row(
    row: JobApplyRow, run: JobApplyRun, owner: UserExtra, account_id: Optional[str], cover_letter: bool
) -> ResumeApplication:
    account_id = account_id or _best_profile(row, run)
    if account_id not in {str(s.accountId) for s in run.selection}:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Pick one of this run's profiles.")
    account = await Account.get(PydanticObjectId(account_id))
    if account is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="That profile no longer exists.")
    job = await resume_queue.enqueue_generation(
        user=owner,
        account=account,
        jd=row.jdText or "",
        company=row.company or _host(row.url) or row.title,
        job_url=row.url,
        cover_letter=cover_letter,
        source="job_applies",
        job_apply_row_id=row.id,
    )
    jobs = ResumeApplication.get_motor_collection()
    # Compare-and-set: only link the new job if the row still points where we read it. A concurrent tailor
    # (double click, `t` twice) that got there first wins; ours is dropped so nothing is left orphaned.
    linked = await JobApplyRow.get_motor_collection().update_one(
        {"_id": row.id, "tailoredJobId": row.tailoredJobId},
        {"$set": {"tailoredJobId": job.id, "tailoredAccountId": account_id, "updatedAt": datetime.utcnow()}},
    )
    if not linked.modified_count:
        await jobs.delete_one({"_id": job.id})
        fresh = await JobApplyRow.get(row.id)
        current = await ResumeApplication.get(fresh.tailoredJobId) if fresh and fresh.tailoredJobId else None
        return current or job
    # Replace the previous tailored job: drop it if it never started and no applied mark uses it.
    if row.tailoredJobId and not any(m.tailoredJobId == str(row.tailoredJobId) for m in row.appliedResumes):
        await jobs.delete_one({"_id": row.tailoredJobId, "status": "queued"})
    return job


class TailorBody(BaseModel):
    accountId: Optional[str] = None
    coverLetter: bool = False


@router.post("/rows/{row_id}/tailor")
async def tailor_row(row_id: str, body: TailorBody, user: Annotated[UserExtra, Depends(get_current_user)]) -> dict:
    """Queue a tailored resume for this job (profile's HTML template + this job's description)."""
    row = await _owned_row(row_id, user)
    run = await JobApplyRun.get(row.runId)
    owner = await UserExtra.get(row.userId)
    if run is None or owner is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    job = await _tailor_row(row, run, owner, body.accountId, body.coverLetter)
    doc = await ResumeApplication.get_motor_collection().find_one({"_id": job.id}, _TAILORED_PROJECTION)
    return {"tailored": _tailored_out(doc)}


@router.post("/runs/{run_id}/tailor-all")
async def tailor_all(run_id: str, body: TailorBody, user: Annotated[UserExtra, Depends(get_current_user)]) -> dict:
    """Queue tailored resumes for every job still to apply to that has none yet (or only a failed one), up to the
    daily cap. Uses the profile filter, else each job's top suggestion's profile."""
    run = await _owned_run(run_id, user)
    owner = await UserExtra.get(run.userId)
    rows = await JobApplyRow.find(
        await _rows_query(run, view="suggested", account_id=body.accountId or "", applied="no")
    ).sort("+rowIndex").to_list()
    existing = await _tailored_docs([r.tailoredJobId for r in rows])
    remaining = await resume_queue.remaining_today(owner)
    queued = skipped = other = 0
    for row in rows:
        current = existing.get(str(row.tailoredJobId))
        if current and current.get("status") != "failed":
            continue
        if remaining <= 0:
            skipped += 1
            continue
        tops = [s.accountId for s in row.bestByProfile if s.total >= run.threshold and (not body.accountId or s.accountId == body.accountId)]
        tops += [a for a in row.tailorOnly if not body.accountId or a == body.accountId]
        if not tops:
            continue
        try:
            await _tailor_row(row, run, owner, tops[0], body.coverLetter)
        except HTTPException as exc:
            if exc.status_code == status.HTTP_429_TOO_MANY_REQUESTS:
                skipped += 1
                remaining = 0
            else:  # e.g. the profile has no HTML template, or the description is too long: skip this job
                other += 1
            continue
        queued += 1
        remaining -= 1
    return {"queued": queued, "skippedCap": skipped, "skipped": other}
