"""Job Applies: upload a sheet of job postings, score selected resumes, suggest one resume per profile."""
import asyncio
import json
import logging
from datetime import date, datetime, timezone
from typing import Annotated, Any, Optional

import httpx
from pymongo import UpdateOne
from beanie import PydanticObjectId
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from pydantic import BaseModel, Field

from app.deps import get_current_user
from app import google_sheets, resume_files, resume_queue
from app.job_apply import file_links, gsheet, sheet_checks, worker
from app.job_apply.sheet_checks import sheet_text
from app.job_apply.fetch import route as fetch_route
from app.job_apply.screening import (
    APPROVABLE, MARKETS, NO_LOCATION, WORTH, bucket, build_report, markets_for, rescreen, scoring_key,
)
from app.job_apply.resume_parse import health
from app.job_apply.sheet import MAX_BYTES, SheetError, parse_sheet
from app.models import Account, ResumeApplication, UserExtra
from app.job_apply.schema import Clearance, GateResult, JobExtraction, Location, WorkAuth, WorkMode
from app.models.job_apply import (
    AppliedMark,
    file_key,
    JobApplicationLog,
    JobApplyRow,
    JobApplyRun,
    JobInfo,
    ProfileGates,
    RunCounts,
    RunSelection,
)
from app.routers._util import doc_to_dict, require_model, to_object_id

log = logging.getLogger(__name__)

router = APIRouter(prefix="/job-applies", tags=["job-applies"])

ACTIVE = ("queued", "running")
FAILED = ("fetch_failed", "llm_failed")


# ---------- helpers ----------


def _can_see(owner_id: PydanticObjectId, user: UserExtra) -> bool:
    return user.role == "admin" or str(owner_id) == str(user.id)


def _retryable_query(run_id: PydanticObjectId) -> dict[str, Any]:
    return {"runId": run_id, "status": {"$in": list(FAILED)}, "screen": {"$ne": "closed"}}


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


async def _run_tailored_ids(run: JobApplyRun) -> list:
    ids = []
    async for doc in JobApplyRow.get_motor_collection().find({"runId": run.id, "tailored": {"$ne": {}}}, {"tailored": 1}):
        ids.extend(v for v in (doc.get("tailored") or {}).values() if v)
    return ids


async def _suggested_count(run: JobApplyRun) -> int:
    return await JobApplyRow.find(
        {"runId": run.id, "$or": [{"topScore": {"$gte": run.threshold}}, {"tailorOnly.0": {"$exists": True}}]}
    ).count()


async def _run_out(run: JobApplyRun, *, with_profiles: bool = False) -> dict:
    out = doc_to_dict(run)
    out["suggested"] = await _suggested_count(run)
    # Failed jobs a retry can help (a closed job stays closed).
    out["retryable"] = await JobApplyRow.find(_retryable_query(run.id)).count()
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
    jdStructured: Optional[bool] = None
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
    tailored: dict[str, PydanticObjectId] = Field(default_factory=dict)
    exported: dict[str, datetime] = Field(default_factory=dict)
    tailorOnly: list[str] = Field(default_factory=list)
    screen: Optional[str] = None
    groupKey: Optional[str] = None
    forceInclude: bool = False

    class Settings:
        projection = {
            "_id": 1, "rowIndex": 1, "url": 1, "title": 1, "company": 1, "status": 1, "statusReason": 1,
            "screen": 1, "groupKey": 1, "forceInclude": 1,
            "jdSource": 1, "jdStructured": 1, "postedDate": 1, "extractionSource": 1, "gates": 1, "profileGates": 1, "topScore": 1,
            "applied": 1, "appliedResumes": 1, "tailored": 1, "exported": 1, "tailorOnly": 1,
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


def _open_profiles(row) -> list[str]:
    return [pg.accountId for pg in row.profileGates if not any(g.result == "fail" for g in pg.gates)]


def _tailored_ids(row) -> list:
    return list(row.tailored.values())


def _row_summary(
    row: JobApplyRow,
    threshold: int,
    account_id: str = "",
    previous: Optional[list] = None,
    tailored_docs: Optional[dict[str, dict]] = None,
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
        "screen": row.screen,
        "groupKey": row.groupKey,
        "markets": markets_for(row.groupKey),
        "forceInclude": row.forceInclude,
        "jdSource": row.jdSource,
        "jdStructured": row.jdStructured,
        "postedDate": row.postedDate.isoformat() if row.postedDate else None,
        "workMode": ex.workMode if ex else None,
        "allowedLocations": [l.model_dump() for l in ex.allowedLocations] if ex else [],
        "timezoneNote": ex.timezoneNote if ex else None,
        "extractionSource": row.extractionSource,
        "humanEdited": row.extractionSource == "human",
        "gates": [g.model_dump() for g in row.gates],
        "profileGates": [pg.model_dump() for pg in row.profileGates],
        "topScore": row.topScore,
        "suggestions": suggestions,
        "appliedResumes": marks,
        "applied": bool(marks),
        # Applications to this URL recorded in other runs (permanent applied log).
        "previouslyApplied": previous or [],
        # One tailored resume per profile: [{jobId, accountId, status, step, hasPdf, error}].
        "tailored": [
            t for t in (_tailored_out((tailored_docs or {}).get(str(jid))) for jid in row.tailored.values()) if t
        ],
        # Profiles without uploaded resumes this job is open to (apply with a tailored resume).
        "tailorOnly": row.tailorOnly,
        # Every profile this job is open to (passes its location / work-authorization checks).
        "openProfiles": _open_profiles(row),
        # Profiles whose row for this job is already in the Google Sheet.
        "exportedProfiles": list(row.exported),
    }


# ---------- applications (job × profile) ----------


class _AppRowView(BaseModel):
    """The fields that decide a job's applications (see `_applications`)."""
    id: PydanticObjectId = Field(alias="_id")
    url: Optional[str] = None
    profileGates: list[ProfileGates] = Field(default_factory=list)
    bestByProfile: list[_ScoreBrief] = Field(default_factory=list)
    applied: bool = False
    appliedResumes: list[AppliedMark] = Field(default_factory=list)
    tailored: dict[str, PydanticObjectId] = Field(default_factory=dict)
    tailorOnly: list[str] = Field(default_factory=list)
    exported: dict[str, datetime] = Field(default_factory=dict)

    class Settings:
        projection = {
            "_id": 1, "url": 1, "profileGates": 1, "applied": 1, "appliedResumes": 1, "tailored": 1, "tailorOnly": 1,
            "exported": 1,
            "bestByProfile.accountId": 1, "bestByProfile.resumeId": 1, "bestByProfile.filename": 1, "bestByProfile.total": 1,
        }


class _AppContext:
    """What deciding application states needs beyond the rows: their tailored resumes, and which profiles
    applied to these URLs in other runs (permanent log)."""

    def __init__(self, tailored: dict[str, dict], logged: set[tuple[str, str]]):
        self.tailored = tailored
        self.logged = logged
        self.logged_urls = {u for u, _ in logged}


async def _app_context(run: JobApplyRun, rows: list) -> _AppContext:
    tailored = await _tailored_docs([i for r in rows for i in _tailored_ids(r)])
    urls = list({r.url for r in rows if r.url})
    logged: set[tuple[str, str]] = set()
    if urls:
        cursor = JobApplicationLog.get_motor_collection().find(
            {"userId": run.userId, "url": {"$in": urls}, "runId": {"$ne": run.id}}, {"url": 1, "accountId": 1}
        )
        async for e in cursor:
            logged.add((e["url"], str(e.get("accountId") or "")))
    return _AppContext(tailored, logged)


def _marked_profiles(row, threshold: int) -> set[str]:
    marked = {m.accountId for m in row.appliedResumes}
    if row.applied and not marked:  # legacy single tick = the top suggestion
        top = next((s for s in row.bestByProfile if s.total >= threshold), None)
        if top:
            marked.add(top.accountId)
    return marked


def _applications(row, threshold: int, ctx: _AppContext, account_id: str = "") -> list[dict]:
    """The job's applications: one per profile it's open to that has a matching uploaded resume, has no uploaded
    resumes (tailor-only), or has a tailored resume for it. State, in order: `applied` (marked in this run, or
    this URL with this profile in another run), `ready` (tailored resume done, else the matching uploaded one),
    `tailoring`, `needsResume`. A ready application carries the file to apply with."""
    matching: dict[str, Any] = {}
    for s in row.bestByProfile:
        if s.total >= threshold:
            matching.setdefault(s.accountId, s)
    marked = _marked_profiles(row, threshold)
    out = []
    for acc in _open_profiles(row):
        if account_id and acc != account_id:
            continue
        t = ctx.tailored.get(str(row.tailored[acc])) if acc in row.tailored else None
        s = matching.get(acc)
        if not (s or t or acc in row.tailorOnly):
            continue
        app: dict[str, Any] = {"accountId": acc, "score": s.total if s else None}
        if acc in marked or (row.url, acc) in ctx.logged:
            app["state"] = "applied"
        elif t and t.get("status") == "completed":
            app.update(state="ready", tailoredJobId=str(t["_id"]), filename=t.get("pdfFilename") or "Tailored resume")
        elif t and t.get("status") in ("queued", "in_progress"):
            app["state"] = "tailoring"
        elif s:
            app.update(state="ready", resumeId=s.resumeId, filename=s.filename)
        else:
            app["state"] = "needsResume"
        out.append(app)
    return out


def _job_applied(row, apps: list[dict], ctx: _AppContext) -> bool:
    """A job is applied when every one of its applications is. A job with none (e.g. under All) counts as applied
    once anything was marked on it, here or in another run."""
    if apps:
        return all(a["state"] == "applied" for a in apps)
    return bool(row.appliedResumes or row.applied or row.url in ctx.logged_urls)


async def _application_counts(run: JobApplyRun) -> dict:
    rows = await JobApplyRow.find(await _rows_query(run, view="suggested")).project(_AppRowView).to_list()
    ctx = await _app_context(run, rows)
    counts = dict.fromkeys(("applied", "ready", "tailoring", "needsResume"), 0)
    per_profile = {str(s.accountId): {"toGo": 0, "ready": 0} for s in run.selection}
    jobs = to_export = 0
    for row in rows:
        apps = _applications(row, run.threshold, ctx)
        pending = [a for a in apps if a["state"] != "applied"]
        jobs += bool(pending)
        to_export += sum(a["accountId"] not in row.exported and a["state"] == "ready" for a in pending)
        for a in apps:
            counts[a["state"]] += 1
        for a in pending:
            mine = per_profile.setdefault(a["accountId"], {"toGo": 0, "ready": 0})
            mine["toGo"] += 1
            mine["ready"] += a["state"] == "ready"
    names = {str(a.id): a.name for a in await Account.find({"_id": {"$in": [s.accountId for s in run.selection]}}).to_list()}
    return {
        "jobs": jobs,  # jobs with at least one application to go
        "toGo": counts["ready"] + counts["tailoring"] + counts["needsResume"],
        "toExport": to_export,  # applications with a resume to send, not yet in the shared sheet
        **counts,
        "byProfile": [
            {"accountId": acc, "name": names.get(acc, "(deleted profile)"), **n} for acc, n in per_profile.items()
        ],
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
        if acc is None or acc.archived or not _can_see(acc.createdBy, user):
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
    run: JobApplyRun, *, view: str = "all", account_id: str = "", min_score: Optional[int] = None
) -> dict[str, Any]:
    """The rows filter behind the table and the counts. To apply / Applied is per application, filtered after
    (see `_applications`)."""
    q: dict[str, Any] = {"runId": run.id}
    floor = max(run.threshold, min_score or 0)
    if view == "suggested":
        q["$or"] = [{"topScore": {"$gte": floor}}, {"tailorOnly.0": {"$exists": True}}]
    elif view == "excluded":
        # Failed a job check, or no profile can take it (every scored profile blocked by location/work
        # authorization, and no tailor-only profile open to it).
        q["$or"] = [
            {"status": "excluded"},
            {"status": "scored", "topScore": None, "tailorOnly.0": {"$exists": False}},
        ]
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
    counts = {
        "links": getattr(rows, "links", len(rows)), "duplicates": getattr(rows, "duplicates", 0),
        "cleaned": getattr(rows, "cleaned", 0),
        # LinkedIn, Indeed, Glassdoor…: never fetched, so they need a description in the sheet or another link
        "blocked": sum(1 for r in rows if not r.jdText and fetch_route(r.url)[0] == "blocked"),
    }
    return {"title": title, "total": len(rows), "withDescription": with_jd, "urlOnly": len(rows) - with_jd, **counts}


@router.post("/runs", status_code=status.HTTP_202_ACCEPTED)
async def create_run(
    user: Annotated[UserExtra, Depends(get_current_user)],
    selection: str = Form(""),
    file: Optional[UploadFile] = File(None),
    sheetUrl: str = Form(""),
    threshold: int = Form(75),
    maxAgeDays: int = Form(30),
) -> dict:
    """The job sheet comes either as an uploaded file or as a link-shared Google Sheet (`sheetUrl`). Without a
    `selection` the run screens the jobs and waits at "screened" for profiles per location group (POST /start);
    with one it scores straight after screening, every group against every selected profile."""
    if await JobApplyRun.find_one({"userId": user.id, "status": {"$in": list(ACTIVE)}}):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A run is already in progress. Wait for it or cancel it.")
    sel = await _parse_selection(selection, user) if selection.strip() else []
    source_url = sheetUrl.strip() or None
    file_name, rows = await _read_sheet(file, source_url)
    if not rows:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No job rows with a URL were found.")

    run = JobApplyRun(
        userId=user.id,
        fileName=file_name[:200],
        sourceUrl=source_url,
        selection=sel,
        autoStart=bool(sel),
        assignments={"*": [s.accountId for s in sel]} if sel else {},
        threshold=max(0, min(100, threshold)),
        maxAgeDays=max(1, min(365, maxAgeDays)),
        counts=RunCounts(total=len(rows)),
    )
    await run.insert()
    await JobApplyRow.insert_many([
        JobApplyRow(
            runId=run.id, userId=user.id, rowIndex=r.rowIndex, url=r.url, jdText=r.jdText,
            jdSource="sheet" if r.jdText else None,
            jdStructured=True if r.jdText else None,
        )
        for r in rows
    ])
    if worker.worker_enabled():
        worker.start_run(run.id)
    return {"runId": str(run.id), "status": run.status, "total": len(rows)}


async def _run_summaries(runs: list[JobApplyRun]) -> dict[str, Optional[dict]]:
    """Per run, the numbers its card shows: jobs worth applying to, closed, per candidate market, and (scored runs)
    jobs still to apply to / applied, tailored resumes in progress. Counts are per job. Two aggregations for the
    whole list; on failure every summary is None and the cards show without numbers."""
    ids = [r.id for r in runs]
    out: dict[str, dict] = {
        str(i): {"worth": 0, "closed": 0, "markets": {m: 0 for m in MARKETS}, "toApply": 0, "applied": 0, "tailoring": 0}
        for i in ids
    }
    if not ids:
        return out
    try:
        pipeline = [
            {"$match": {"runId": {"$in": ids}}},
            {"$lookup": {"from": "job_apply_runs", "localField": "runId", "foreignField": "_id", "as": "run",
                         "pipeline": [{"$project": {"threshold": 1}}]}},
            {"$set": {
                "thr": {"$first": "$run.threshold"},
                "isApplied": {"$or": [{"$eq": ["$applied", True]}, {"$gt": [{"$size": {"$ifNull": ["$appliedResumes", []]}}, 0]}]},
            }},
            {"$set": {"isSuggested": {"$or": [
                {"$and": [{"$ne": ["$topScore", None]}, {"$gte": ["$topScore", "$thr"]}]},
                {"$gt": [{"$size": {"$ifNull": ["$tailorOnly", []]}}, 0]},
            ]}}},
            {"$group": {
                "_id": {"run": "$runId", "screen": "$screen", "key": "$groupKey"},
                "n": {"$sum": 1},
                "toApply": {"$sum": {"$cond": [{"$and": ["$isSuggested", {"$not": ["$isApplied"]}]}, 1, 0]}},
                "applied": {"$sum": {"$cond": ["$isApplied", 1, 0]}},
            }},
        ]
        async for g in JobApplyRow.get_motor_collection().aggregate(pipeline):
            s = out[str(g["_id"]["run"])]
            screen = g["_id"].get("screen")
            if screen in WORTH:
                s["worth"] += g["n"]
                for m in markets_for(g["_id"].get("key")):
                    s["markets"][m] += g["n"]
            elif screen == "closed":
                s["closed"] += g["n"]
            s["toApply"] += g["toApply"]
            s["applied"] += g["applied"]
        tailoring = [
            {"$match": {"userId": runs[0].userId, "jobApplyRowId": {"$ne": None}, "status": {"$in": ["queued", "in_progress"]}}},
            {"$lookup": {"from": "job_apply_rows", "localField": "jobApplyRowId", "foreignField": "_id", "as": "row",
                         "pipeline": [{"$project": {"runId": 1}}]}},
            {"$group": {"_id": {"$first": "$row.runId"}, "n": {"$sum": 1}}},
        ]
        async for g in ResumeApplication.get_motor_collection().aggregate(tailoring):
            if g["_id"] is not None and str(g["_id"]) in out:
                out[str(g["_id"])]["tailoring"] = g["n"]
    except Exception as exc:  # noqa: BLE001 — the list still loads; cards show without numbers
        log.warning("job-apply run summaries failed: %s", exc)
        return {str(i): None for i in ids}
    return out


@router.get("/runs")
async def list_runs(user: Annotated[UserExtra, Depends(get_current_user)]) -> dict:
    # every run still kept (RUN_RETENTION_DAYS): the page filters and pages them
    runs = await JobApplyRun.find({"userId": user.id}).sort("-createdAt").limit(200).to_list()
    summaries = await _run_summaries(runs)
    return {"runs": [{**await _run_out(r), "summary": summaries.get(str(r.id))} for r in runs]}


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
    # Applications = job × profile: what's left to apply with, per profile and per state.
    out["applications"] = await _application_counts(run)
    out["toApply"] = out["applications"]["jobs"]
    # Matches the Excluded tab (counts.excluded only counts jobs that failed a job-level check).
    out["excludedCount"] = await JobApplyRow.find(await _rows_query(run, view="excluded")).count()
    tailored_ids = await _run_tailored_ids(run)
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
    threshold: Optional[int] = Field(None, ge=0, le=100)
    maxAgeDays: Optional[int] = Field(None, ge=1, le=365)


class _RescreenView(BaseModel):
    """What re-applying the job gates reads (not the job text or scores)."""

    id: PydanticObjectId = Field(alias="_id")
    status: str
    statusReason: Optional[str] = None
    extraction: Optional[JobExtraction] = None
    postedDate: Optional[date] = None
    gates: list[GateResult] = Field(default_factory=list)
    screen: Optional[str] = None
    groupKey: Optional[str] = None
    forceInclude: bool = False
    jdStructured: Optional[bool] = None

    class Settings:
        projection = {
            "_id": 1, "status": 1, "statusReason": 1, "extraction": 1, "postedDate": 1, "gates": 1, "screen": 1,
            "groupKey": 1, "forceInclude": 1, "jdStructured": 1,
        }


async def _rescreen_rows(run: JobApplyRun) -> None:
    """Re-apply the job gates to every read row (max age changed). No fetch, no LLM; one write for the rows
    whose result changed."""
    run_date = run.runDate.date()
    ops, excluded = [], 0
    rows = await JobApplyRow.find({"runId": run.id, "extraction": {"$ne": None}}).project(_RescreenView).to_list()
    for row in rows:
        before = (row.status, row.statusReason, row.screen, [g.model_dump() for g in row.gates])
        rescreen(row, run_date=run_date, max_age_days=run.maxAgeDays)
        excluded += row.status == "excluded"
        after = (row.status, row.statusReason, row.screen, [g.model_dump() for g in row.gates])
        if after != before:
            ops.append(UpdateOne({"_id": row.id}, {"$set": {
                "gates": after[3], "status": row.status, "statusReason": row.statusReason, "screen": row.screen,
                "updatedAt": datetime.utcnow(),
            }}))
    if ops:
        await JobApplyRow.get_motor_collection().bulk_write(ops, ordered=False)
    await JobApplyRun.get_motor_collection().update_one({"_id": run.id}, {"$set": {"counts.excluded": excluded}})


@router.patch("/runs/{run_id}")
async def update_run(run_id: str, body: RunPatch, user: Annotated[UserExtra, Depends(get_current_user)]) -> dict:
    run = await _owned_run(run_id, user)
    if body.maxAgeDays is not None and run.status != "screened":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Max age can only change before scoring starts.")
    fields: dict[str, Any] = {k: v for k, v in body.model_dump().items() if v is not None}
    # Targeted $set: a full save() would overwrite counts/status the worker is updating meanwhile.
    await JobApplyRun.get_motor_collection().update_one(
        {"_id": run.id}, {"$set": {**fields, "updatedAt": datetime.utcnow()}}
    )
    for k, v in fields.items():
        setattr(run, k, v)
    if body.maxAgeDays is not None:
        await _rescreen_rows(run)
    return await _run_out(run, with_profiles=True)


class _ScreenView(BaseModel):
    """The few fields the screening report reads (never the job text: runs have up to ~1,000 rows)."""

    screen: Optional[str] = None
    groupKey: Optional[str] = None
    forceInclude: bool = False


@router.get("/runs/{run_id}/screening")
async def get_screening(run_id: str, user: Annotated[UserExtra, Depends(get_current_user)]) -> dict:
    """How many jobs are worth applying to and why the others aren't, plus the worthwhile jobs grouped by location
    with the run owner's profiles that fit each group."""
    run = await _owned_run(run_id, user)
    rows = await JobApplyRow.find({"runId": run.id}).project(_ScreenView).to_list()
    accounts = await Account.find({"createdBy": run.userId, "archived": {"$ne": True}}).sort("+name").to_list()
    profiles = [
        {"id": str(a.id), "name": a.name, "country": a.country, "region": a.region, "resumes": len(a.resumes)}
        for a in accounts
    ]
    return build_report(
        [(r.screen, scoring_key(r.groupKey, r.forceInclude)) for r in rows], profiles, max_age_days=run.maxAgeDays
    )


class StartBody(BaseModel):
    assignments: dict[str, list[str]]
    selection: list[dict[str, Any]]
    threshold: int = Field(75, ge=0, le=100)


# Scoring results on a row; cleared when the run is scored again with new picks. Applied marks, tailored resumes
# and sheet exports belong to a job x profile, not to a score, so they stay.
_SCORE_RESET = {"scores": [], "profileGates": [], "bestByProfile": [], "topScore": None, "tailorOnly": [], "scoredAt": None}


async def _clear_scores(run_id: PydanticObjectId) -> None:
    coll = JobApplyRow.get_motor_collection()
    now = datetime.utcnow()
    scored = {"runId": run_id, "status": {"$in": ["scored", "unassigned"]}}
    # an approved job that failed a check goes back to excluded (scoring takes both); one that only needed a check
    # goes back to extracted like any other
    failed = {"gates": {"$elemMatch": {"result": "fail"}}}
    await coll.update_many(
        {**scored, "forceInclude": True, **failed}, {"$set": {**_SCORE_RESET, "status": "excluded", "updatedAt": now}}
    )
    await coll.update_many(scored, {"$set": {**_SCORE_RESET, "status": "extracted", "updatedAt": now}})


@router.post("/runs/{run_id}/start")
async def start_run(run_id: str, body: StartBody, user: Annotated[UserExtra, Depends(get_current_user)]) -> dict:
    """Score a screened run: each location group's jobs against the profiles picked for it. A run that has finished
    scoring can be scored again with new picks (back to step 2): its scores are replaced."""
    run = await _owned_run(run_id, user)
    rescore = run.phase == "score" and run.status in ("done", "failed", "cancelled")
    if run.phase == "score" and run.status in ACTIVE:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="The run is still scoring.")
    if run.status != "screened" and not rescore:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Run is not waiting for review.")
    if await JobApplyRun.find_one({"userId": run.userId, "status": {"$in": list(ACTIVE)}}):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Another run is in progress.")
    sel = await _parse_selection(json.dumps(body.selection), user)
    picked = {str(s.accountId) for s in sel}
    assignments = {k: list(dict.fromkeys(ids)) for k, ids in body.assignments.items() if ids}
    if any(v not in picked for ids in assignments.values() for v in ids):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Pick resumes for every assigned profile.")
    worth = await JobApplyRow.find({"runId": run.id, "screen": {"$in": list(WORTH)}}).project(_ScreenView).to_list()
    keys = {k for r in worth for k in (markets_for(scoring_key(r.groupKey, r.forceInclude)) or [NO_LOCATION])}
    if not keys or not ("*" in assignments or keys & set(assignments)):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Pick at least one profile for a group with jobs.")
    fields = {
        "assignments": {k: [PydanticObjectId(v) for v in ids] for k, ids in assignments.items()},
        "selection": [s.model_dump() for s in sel], "threshold": body.threshold,
        "phase": "score", "status": "queued", "updatedAt": datetime.utcnow(),
    }
    if rescore:
        fields.update({"counts.scored": 0, "error": None, "finishedAt": None, "heartbeatAt": None})
    res = await JobApplyRun.get_motor_collection().update_one({"_id": run.id, "status": run.status}, {"$set": fields})
    if not res.modified_count:  # a second click got here first
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Run is not waiting for review.")
    if rescore:
        await _clear_scores(run.id)
    if worker.worker_enabled():
        worker.start_run(run.id)
    return await _run_out(await JobApplyRun.get(run.id), with_profiles=True)


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
        _retryable_query(run.id),
        {"$set": {"status": "pending", "statusReason": None, "screen": None, "updatedAt": datetime.utcnow()}},
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
    tailored_ids = await _run_tailored_ids(run)
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
    screen: str = Query(""),
) -> dict:
    run = await _owned_run(run_id, user)
    q = await _rows_query(run, view=view, account_id=accountId, min_score=minScore)
    if screen:
        q["screen"] = screen

    if applied == "any":
        total = await JobApplyRow.find(q).count()
        rows = await (
            JobApplyRow.find(q).sort("+rowIndex").skip((page - 1) * limit).limit(limit).project(_RowListView).to_list()
        )
        ctx = await _app_context(run, rows)
    else:
        # Per application: a job stays To apply until every profile it's for has applied (≤ ~1,000 rows a run).
        light = await JobApplyRow.find(q).sort("+rowIndex").project(_AppRowView).to_list()
        ctx = await _app_context(run, light)
        want = applied == "yes"
        ids = [r.id for r in light if _job_applied(r, _applications(r, run.threshold, ctx, accountId), ctx) == want]
        total = len(ids)
        page_ids = ids[(page - 1) * limit : page * limit]
        rows = await JobApplyRow.find({"_id": {"$in": page_ids}}).sort("+rowIndex").project(_RowListView).to_list()
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
    out = []
    for r in rows:
        summary = _row_summary(r, run.threshold, accountId, previous.get(r.url), ctx.tailored)
        apps = _applications(r, run.threshold, ctx, accountId)
        # [{accountId, state}] per profile this job is for; the job is done when all are applied.
        summary["applications"] = [{"accountId": a["accountId"], "state": a["state"]} for a in apps]
        summary["applied"] = _job_applied(r, apps, ctx)
        out.append(summary)
    return {
        "rows": out,
        "pagination": {"page": page, "limit": limit, "total": total, "totalPages": pages, "hasNext": page < pages, "hasPrev": page > 1},
    }


async def _row_detail(row: JobApplyRow) -> dict:
    run = await JobApplyRun.get(row.runId)
    tailored = await _tailored_docs(_tailored_ids(row))
    out = _row_summary(row, run.threshold if run else 75, tailored_docs=tailored)
    out["scores"] = [s.model_dump(mode="json") for s in sorted(row.scores, key=lambda s: s.total, reverse=True)]
    out["extraction"] = row.extraction.model_dump(mode="json") if row.extraction else None
    out["jdText"] = row.jdText
    # Who last corrected this URL (a correction is shared by everyone who uses the link).
    info = await JobInfo.find_one(JobInfo.url == row.url) if row.url else None
    out["info"] = (
        {
            "editedBy": info.editedByName,
            "editedAt": info.editedAt.isoformat() + "Z",
            # false: this row was read before the correction (see POST /runs/{id}/sync-info).
            "applied": worker.same_time(row.infoAt, info.editedAt),
        }
        if info
        else None
    )
    return out


@router.get("/rows/{row_id}")
async def get_row(row_id: str, user: Annotated[UserExtra, Depends(get_current_user)]) -> dict:
    return await _row_detail(await _owned_row(row_id, user))


class IncludeBody(BaseModel):
    include: bool


@router.put("/rows/{row_id}/include")
async def include_row(row_id: str, body: IncludeBody, user: Annotated[UserExtra, Depends(get_current_user)]) -> dict:
    """Approve a job (or undo it): a person has checked it, so it moves to Worth applying whatever the checks said.
    Needs a readable description: for a job that could not be fetched or read it has to be pasted in Job info first
    (saving it reads the job again, and it lands in the bucket its checks give)."""
    row = await _owned_row(row_id, user)
    run = await JobApplyRun.get(row.runId)
    if run is None or run.status != "screened":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Jobs can only be approved before scoring starts.")
    is_job = row.extraction.isJobPosting if row.extraction is not None else True
    base = bucket(
        status=row.status, status_reason=row.statusReason, gates=row.gates, group_key=row.groupKey, force_include=False,
        is_job=is_job, verified=row.jdStructured is not False,
    )
    if body.include and base not in APPROVABLE:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "This job is already worth applying to."
                if base == "valid"
                else "Paste the job description in Job info first: there is nothing to approve until the job has been read."
            ),
        )
    row.forceInclude = body.include
    row.screen = bucket(
        status=row.status, status_reason=row.statusReason, gates=row.gates, group_key=row.groupKey,
        force_include=row.forceInclude, is_job=is_job, verified=row.jdStructured is not False,
    )
    await JobApplyRow.get_motor_collection().update_one(
        {"_id": row.id}, {"$set": {"forceInclude": row.forceInclude, "screen": row.screen, "updatedAt": datetime.utcnow()}}
    )
    return _row_summary(row, run.threshold)


# ---------- job info corrections ----------


@router.post("/runs/{run_id}/sync-info")
async def sync_info(run_id: str, user: Annotated[UserExtra, Depends(get_current_user)]) -> dict:
    """Re-read this run's jobs whose link was corrected by someone after they were read (no fetch, no LLM)."""
    run = await _owned_run(run_id, user)
    return {"updated": await worker.sync_corrections(run)}


class InfoBody(BaseModel):
    """What a person corrected. Only the fields sent change; the description may be replaced or, for a job that
    could not be fetched, pasted."""

    title: Optional[str] = Field(default=None, max_length=300)
    company: Optional[str] = Field(default=None, max_length=300)
    workMode: Optional[WorkMode] = None
    allowedLocations: Optional[list[Location]] = None
    postedDate: Optional[date] = None  # null clears it
    clearance: Optional[str] = Field(default=None, pattern="^(required|preferred|none)$")
    workAuthorization: Optional[WorkAuth] = None  # null clears it
    timezoneNote: Optional[str] = Field(default=None, max_length=300)  # null clears it
    jdText: Optional[str] = Field(default=None, max_length=60000)


_NULLABLE = {"postedDate", "workAuthorization", "timezoneNote"}


def _require_editor(user: UserExtra) -> None:
    if user.role not in ("admin", "staff"):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Only admin and staff can correct jobs.")


@router.put("/rows/{row_id}/info")
async def update_row_info(row_id: str, body: InfoBody, user: Annotated[UserExtra, Depends(get_current_user)]) -> dict:
    """Correct a job's parsed info and/or description. Saved per URL for everyone; this row is read again now,
    other runs use it the next time they read the URL."""
    _require_editor(user)
    row = await _owned_row(row_id, user)
    if not row.url:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only jobs with a link can be corrected.")
    run = await JobApplyRun.get(row.runId)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    if run.status in ACTIVE:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Wait for the run to finish its current step.")

    info = await JobInfo.find_one(JobInfo.url == row.url)
    sent = {f for f in body.model_fields_set if getattr(body, f) is not None or f in _NULLABLE}
    current_jd = (info.jdText if info else row.jdText) or ""
    new_jd = (body.jdText or "").strip() if "jdText" in sent else ""
    jd = new_jd or current_jd
    if not jd.strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Paste the job description first.")
    fields = sent - {"jdText"}
    ex = info.extraction if info else row.extraction
    if ex is None or (new_jd and new_jd != current_jd and not fields):
        # No reading yet, or a new description with nothing else corrected: read it (one LLM call).
        try:
            ex, _ = await worker.EXTRACT(jd, user=user, run_date=run.runDate.date())
        except Exception as exc:  # noqa: BLE001
            log.warning("job info extraction failed: %s", exc)
            raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Could not read that description.") from exc
    ex = ex.model_copy(deep=True)
    ex.isJobPosting = True
    if "title" in fields:
        ex.title = (body.title or "").strip()
    elif not ex.title:
        ex.title = row.title
    if "company" in fields:
        ex.company = (body.company or "").strip()
    elif not ex.company:
        ex.company = row.company
    if "workMode" in fields:
        ex.workMode = body.workMode
    if "allowedLocations" in fields:
        ex.allowedLocations = body.allowedLocations
    if "postedDate" in fields:
        ex.postedDate = body.postedDate
    if "clearance" in fields:
        ex.clearance = Clearance(level=body.clearance, note=ex.clearance.note)
    if "workAuthorization" in fields:
        ex.workAuthorization = body.workAuthorization
    if "timezoneNote" in fields:
        ex.timezoneNote = (body.timezoneNote or "").strip() or None

    if info is None:
        info = JobInfo(url=row.url, jdText=jd, extraction=ex)
    info.jdText, info.extraction, info.postedDate = jd, ex, ex.postedDate
    info.title, info.company = ex.title, ex.company
    info.editedBy, info.editedByName, info.editedAt = user.id, user.name or user.email or "", datetime.utcnow()
    await info.save()

    await worker.reapply_info(run, row)  # also moves the run's counters
    return await _row_detail(row)


@router.delete("/rows/{row_id}/info")
async def reset_row_info(row_id: str, user: Annotated[UserExtra, Depends(get_current_user)]) -> dict:
    """Drop the correction for this job's URL and read the job again with the machine (fetch + LLM)."""
    _require_editor(user)
    row = await _owned_row(row_id, user)
    run = await JobApplyRun.get(row.runId)
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    if run.status in ACTIVE or await JobApplyRun.find_one({"userId": run.userId, "status": {"$in": list(ACTIVE)}}):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A run is in progress.")
    if row.url:
        await JobInfo.find(JobInfo.url == row.url).delete()
    before = worker.row_counts(row)
    if row.scoredAt:
        await JobApplyRun.get_motor_collection().update_one({"_id": run.id}, {"$inc": {"counts.scored": -1}})
    await JobApplyRow.get_motor_collection().update_one(
        {"_id": row.id},
        {"$set": {
            "status": "pending", "statusReason": None, "screen": None, "jdText": None, "jdSource": None,
            "jdStructured": None, "fetchedPostedDate": None, "postedDate": None, "title": "", "company": "",
            "extraction": None, "extractionSource": None, "infoAt": None, "gates": [], "groupKey": None, "scoredAt": None,
            "scores": [], "profileGates": [], "bestByProfile": [], "topScore": None, "tailorOnly": [],
            "updatedAt": datetime.utcnow(),
        }},
    )
    await worker.move_counts(run.id, before, {k: 0 for k in before})
    await JobApplyRun.get_motor_collection().update_one(
        {"_id": run.id},
        {"$set": {"status": "queued", "error": None, "finishedAt": None, "heartbeatAt": None, "updatedAt": datetime.utcnow()}},
    )
    if worker.worker_enabled():
        worker.start_run(run.id)
    return {"ok": True}


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
        if job is None or body.tailoredJobId not in {str(i) for i in _tailored_ids(row)}:
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
    # True = every suggested job in this run (all pages), instead of rowIds.
    all: bool = False
    # Only this profile's applications.
    accountId: str = ""


@router.post("/runs/{run_id}/mark-top-applied")
async def mark_top_applied(
    run_id: str, body: BulkAppliedBody, user: Annotated[UserExtra, Depends(get_current_user)]
) -> dict:
    """Mark every ready application applied (each profile with the file the export uses: its tailored resume when
    done, else its matching uploaded one) on the selected jobs, or on every suggested job. Applications already
    applied, still tailoring or without a resume are left. Returns the marks made, for Undo."""
    run = await _owned_run(run_id, user)
    if body.all:
        q = await _rows_query(run, view="suggested", account_id=body.accountId)
    else:
        q = {"_id": {"$in": [to_object_id(i) for i in body.rowIds]}, "runId": run.id}
    rows = await JobApplyRow.find(q).sort("+rowIndex").to_list()
    ctx = await _app_context(run, rows)
    marks = []
    for row in rows:
        for app in _applications(row, run.threshold, ctx, body.accountId):
            if app["state"] != "ready":
                continue
            acc = app["accountId"]
            if app.get("tailoredJobId"):
                await _set_applied(row, acc, app["filename"], True, tailored_job_id=app["tailoredJobId"])
                marks.append({"rowId": str(row.id), "accountId": acc, "tailoredJobId": app["tailoredJobId"]})
            else:
                await _set_applied(row, acc, app["filename"], True, resume_id=app["resumeId"])
                marks.append({"rowId": str(row.id), "accountId": acc, "resumeId": app["resumeId"]})
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
    row: JobApplyRow,
    run: JobApplyRun,
    owner: UserExtra,
    account_id: Optional[str],
    cover_letter: bool,
    resume_model: Optional[str] = None,
    cover_letter_model: Optional[str] = None,
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
        resume_model=resume_model,
        cover_letter_model=cover_letter_model,
    )
    jobs = ResumeApplication.get_motor_collection()
    # Compare-and-set: only link the new job if the row still points where we read it. A concurrent tailor
    # (double click, `t` twice) that got there first wins; ours is dropped so nothing is left orphaned.
    previous = row.tailored.get(account_id)
    linked = await JobApplyRow.get_motor_collection().update_one(
        {"_id": row.id, f"tailored.{account_id}": previous},
        {"$set": {f"tailored.{account_id}": job.id, "updatedAt": datetime.utcnow()}},
    )
    if not linked.modified_count:
        await jobs.delete_one({"_id": job.id})
        fresh = await JobApplyRow.get(row.id)
        current_id = fresh.tailored.get(account_id) if fresh else None
        current = await ResumeApplication.get(current_id) if current_id else None
        return current or job
    # Replace this profile's previous tailored job: drop it if it never started and no applied mark uses it.
    if previous and not any(m.tailoredJobId == str(previous) for m in row.appliedResumes):
        await jobs.delete_one({"_id": previous, "status": "queued"})
    row.tailored[account_id] = job.id
    return job


class TailorBody(BaseModel):
    accountId: Optional[str] = None
    coverLetter: bool = False
    resumeModel: Optional[str] = None  # catalog ids (GET /resume/models); blank = server default
    coverLetterModel: Optional[str] = None


class TailorAllBody(BaseModel):
    # Profiles to tailor for (default: every profile in the run); each job gets one per chosen profile it's open to.
    accountIds: Optional[list[str]] = None
    coverLetter: bool = False
    resumeModel: Optional[str] = None
    coverLetterModel: Optional[str] = None
    dryRun: bool = False  # count what would be queued, queue nothing

    # Only these jobs (the ones ticked in the table), instead of every suggested job.
    rowIds: Optional[list[str]] = Field(default=None, max_length=1000)

@router.post("/rows/{row_id}/tailor")
async def tailor_row(row_id: str, body: TailorBody, user: Annotated[UserExtra, Depends(get_current_user)]) -> dict:
    """Queue a tailored resume for this job (profile's HTML template + this job's description)."""
    row = await _owned_row(row_id, user)
    run = await JobApplyRun.get(row.runId)
    owner = await UserExtra.get(row.userId)
    if run is None or owner is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    resume_model = require_model("resume", body.resumeModel, owner)
    cover_letter_model = require_model("cover_letter", body.coverLetterModel, owner)
    job = await _tailor_row(
        row, run, owner, body.accountId, body.coverLetter,
        resume_model=resume_model, cover_letter_model=cover_letter_model,
    )
    doc = await ResumeApplication.get_motor_collection().find_one({"_id": job.id}, _TAILORED_PROJECTION)
    return {"tailored": _tailored_out(doc)}


@router.post("/runs/{run_id}/tailor-all")
async def tailor_all(run_id: str, body: TailorAllBody, user: Annotated[UserExtra, Depends(get_current_user)]) -> dict:
    """Queue a tailored resume for each suggested job (or just the `rowIds` given) × each chosen profile the job is
    open to and that hasn't applied to it yet, unless that profile already has one (a failed one is redone), up to
    the daily cap. `dryRun` only counts."""
    run = await _owned_run(run_id, user)
    owner = await UserExtra.get(run.userId)
    resume_model = require_model("resume", body.resumeModel, owner)
    cover_letter_model = require_model("cover_letter", body.coverLetterModel, owner)
    chosen = set(body.accountIds or [str(s.accountId) for s in run.selection])
    if body.rowIds is not None:  # the ticked jobs of this run, whatever view they were ticked in
        query: dict[str, Any] = {"_id": {"$in": [to_object_id(i) for i in body.rowIds]}, "runId": run.id}
    else:
        query = await _rows_query(run, view="suggested")
    rows = await JobApplyRow.find(query).sort("+rowIndex").to_list()
    ctx = await _app_context(run, rows)
    existing = ctx.tailored
    remaining = await resume_queue.remaining_today(owner)
    queued = skipped = other = 0
    for row in rows:
        done = _marked_profiles(row, run.threshold) | {acc for url, acc in ctx.logged if url == row.url}
        for acc in [a for a in _open_profiles(row) if a in chosen and a not in done]:
            current = existing.get(str(row.tailored.get(acc)))
            if current and current.get("status") != "failed":
                continue
            if remaining <= 0:
                skipped += 1
                continue
            if body.dryRun:
                queued += 1
                remaining -= 1
                continue
            try:
                await _tailor_row(
                    row, run, owner, acc, body.coverLetter,
                    resume_model=resume_model, cover_letter_model=cover_letter_model,
                )
            except HTTPException as exc:
                if exc.status_code == status.HTTP_429_TOO_MANY_REQUESTS:
                    skipped += 1
                    remaining = 0
                else:  # e.g. the profile has no HTML template, or the description is too long
                    other += 1
                continue
            queued += 1
            remaining -= 1
    return {"queued": queued, "skippedCap": skipped, "skipped": other}


# ---------- export to Google Sheets ----------

# One row per application with a resume to send. Re-tailoring fills the new link into the row it went to
# (app/job_apply/sheet_links.py finds it again by Profile and Job URL).
EXPORT_HEADER = ["Profile", "Company Name", "Job Title", "Job URL", "Download Resume"]


class ExportBody(BaseModel):
    sheetUrl: str = ""  # empty = the link saved from the last export
    dryRun: bool = False  # count and check access only


def _download_cell(link: str) -> str:
    return f'=HYPERLINK("{link}","Download")'


def _export_target(sheet_url: str, user: UserExtra) -> tuple[str, str]:
    """(link, spreadsheet id) to export to: the one given, else the one saved from the last export."""
    url = sheet_url.strip() or (user.jobApplySheetUrl or "")
    if not url:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Paste the Google Sheet link to export to.")
    try:
        sheet_id, _ = gsheet.parse_gsheet_url(url)
    except SheetError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    return url, sheet_id


async def _remember_sheet(url: str, user: UserExtra) -> None:
    if url != user.jobApplySheetUrl:
        await UserExtra.get_motor_collection().update_one({"_id": user.id}, {"$set": {"jobApplySheetUrl": url}})


async def _checks_rows(run: JobApplyRun) -> list[list]:
    """The run's Checks tab: every job, in sheet order, with its details and status."""
    rows = await JobApplyRow.find({"runId": run.id}).sort("+rowIndex").to_list()
    return [sheet_checks.checks_row(r) for r in rows]


async def _write_checks(http: httpx.AsyncClient, sheet_id: str, run: JobApplyRun, info: dict) -> tuple[str, int]:
    """Write (or rewrite) the run's own Checks tab; exporting again replaces it, never adds to it."""
    tab = sheet_checks.checks_tab_name(run)
    values = await _checks_rows(run)
    await google_sheets.ensure_tab(http, sheet_id, tab, sheet_checks.CHECKS_HEADER, info)
    # only the export's own columns: notes typed beside them stay next to the same jobs (same order each time)
    await google_sheets.clear_range(http, sheet_id, google_sheets._a1(tab, "A1:H"))
    await google_sheets.update_range(http, sheet_id, google_sheets._a1(tab, "A1"), [sheet_checks.CHECKS_HEADER, *values])
    return tab, len(values)


def _today_label(user: UserExtra) -> str:
    from app.alerts import local_now
    from app.slack_bot import resolve_user_tz

    return local_now(resolve_user_tz(user)).date().isoformat()


@router.post("/runs/{run_id}/export-sheet")
async def export_sheet(run_id: str, body: ExportBody, user: Annotated[UserExtra, Depends(get_current_user)]) -> dict:
    """Append one row per job x profile with a resume to send (that profile's tailored resume when it's done, else its
    matching uploaded one) to today's "Apply · <date>" tab of the shared Google Sheet. Applications waiting for a
    tailored resume aren't written yet: exporting again after tailoring adds them. Rows already exported are skipped."""
    run = await _owned_run(run_id, user)
    url, sheet_id = _export_target(body.sheetUrl, user)

    rows = await JobApplyRow.find(await _rows_query(run, view="suggested")).sort("+rowIndex").to_list()
    ctx = await _app_context(run, rows)
    accounts = {str(a.id): a for a in await Account.find({"_id": {"$in": [s.accountId for s in run.selection]}}).to_list()}
    resumes = {str(r.id): r for a in accounts.values() for r in a.resumes}
    tab = f"Apply · {_today_label(user)}"

    out: list[list] = []
    marks: list[tuple] = []
    waiting = already = no_file = 0
    for row in rows:
        for app in _applications(row, run.threshold, ctx):
            acc = app["accountId"]
            if app["state"] == "applied":
                continue
            if acc in row.exported:
                already += 1
                continue
            if app["state"] != "ready":
                waiting += 1  # tailoring, or needs a tailored resume: exported once it's done
                continue
            if app.get("tailoredJobId"):
                link = file_links.file_url("tailored", app["tailoredJobId"])
            else:
                resume = resumes.get(app["resumeId"])
                if not resume or not resume.s3Key:
                    no_file += 1  # the original PDF isn't stored, so there's nothing to link to
                    continue
                link = file_links.file_url("resume", app["resumeId"], account_id=acc)
            name = accounts[acc].name if acc in accounts else "Profile"
            out.append([*map(sheet_text, (name, row.company, row.title, row.url)), _download_cell(link)])
            marks.append((row.id, acc))

    async with httpx.AsyncClient() as http:
        try:
            info = await google_sheets.sheet_info(http, sheet_id)
            written = None
            if not body.dryRun and out:
                if not await google_sheets.ensure_tab(http, sheet_id, tab, EXPORT_HEADER, info):
                    await google_sheets.update_range(http, sheet_id, google_sheets._a1(tab, "A1"), [EXPORT_HEADER])
                written = await google_sheets.append_rows(http, sheet_id, tab, out)
        except google_sheets.SheetsError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))

    if not body.dryRun:
        now, first = datetime.utcnow(), None
        if written:
            try:
                _, first, _ = google_sheets.parse_row_span(written)
            except ValueError:
                log.warning("export of run %s: unexpected range %r, rows not recorded", run.id, written)
        coll = JobApplyRow.get_motor_collection()
        for i, (row_id, acc) in enumerate(marks):
            fields = {f"exported.{acc}": now, "updatedAt": now}
            if first is not None:
                fields[f"exportedRows.{acc}"] = {"sheetId": sheet_id, "tab": tab, "row": first + i}
            await coll.update_one({"_id": row_id}, {"$set": fields})
    await _remember_sheet(url, user)
    return {
        "ready": len(out),
        "added": 0 if body.dryRun else len(out),
        "waiting": waiting,  # applications without their tailored resume yet
        "alreadyExported": already,
        "noFile": no_file,
        "sheetTitle": info["title"],
        "tab": tab,
        "sheetUrl": url,
        "serviceAccount": google_sheets.service_account_email(),
    }


def _pdf_name(*parts: str) -> str:
    import re

    return re.sub(r"[^\w .()-]+", "_", " - ".join(p for p in parts if p)).strip() or "Resume"


class ChecksBody(BaseModel):
    sheetUrl: str = ""  # empty = the link saved from the last export
    dryRun: bool = False  # count and check access only


@router.post("/runs/{run_id}/export-checks")
async def export_checks(run_id: str, body: ChecksBody, user: Annotated[UserExtra, Depends(get_current_user)]) -> dict:
    """Write every job in the run (company, URL, title, posting date, location, security clearance, remote / hybrid /
    on-site, status) to the run's own "Checks · …" tab, replacing what an earlier export wrote there. Available once
    the jobs are checked (step 2 onward); adds no application rows."""
    run = await _owned_run(run_id, user)
    if run.phase == "screen" and run.status in ("queued", "running"):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="The jobs are still being checked.")
    url, sheet_id = _export_target(body.sheetUrl, user)
    tab = sheet_checks.checks_tab_name(run)
    async with httpx.AsyncClient() as http:
        try:
            info = await google_sheets.sheet_info(http, sheet_id)
            if body.dryRun:
                jobs = await JobApplyRow.find({"runId": run.id}).count()
            else:
                tab, jobs = await _write_checks(http, sheet_id, run, info)
        except google_sheets.SheetsError as exc:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    await _remember_sheet(url, user)
    return {
        "tab": tab,
        "jobs": jobs,
        "sheetTitle": info["title"],
        "sheetUrl": url,
        "serviceAccount": google_sheets.service_account_email(),
    }

@router.get("/files/{token}")
async def resume_file(token: str):
    """A resume PDF from an exported sheet's link. No login: the signed, expiring link is the permission."""
    import base64

    from fastapi.responses import RedirectResponse, Response

    try:
        ref = file_links.read_token(token)
    except file_links.BadLink:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="This link is invalid or has expired.")
    if ref["kind"] == "tailored":
        doc = await ResumeApplication.get_motor_collection().find_one(
            {"_id": to_object_id(ref["id"])}, {"s3Key": 1, "pdfData": 1, "companyName": 1, "profileName": 1}
        )
        if doc is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="This resume no longer exists.")
        filename = _pdf_name(f"Resume ({doc.get('companyName') or 'job'})", doc.get("profileName") or "") + ".pdf"
        if doc.get("s3Key"):
            return RedirectResponse(resume_files.presigned_url(doc["s3Key"], filename))
        if doc.get("pdfData"):
            return Response(
                base64.b64decode(doc["pdfData"]), media_type="application/pdf",
                headers={"Content-Disposition": f'inline; filename="{filename}"'},
            )
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="This resume isn't ready.")
    acc = await Account.get(to_object_id(ref["accountId"] or ""))
    resume = next((r for r in acc.resumes if str(r.id) == ref["id"]), None) if acc else None
    if resume is None or not resume.s3Key:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="This resume file isn't available.")
    return RedirectResponse(resume_files.presigned_url(resume.s3Key, resume.filename))
