import asyncio
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Annotated, Any, Iterable, List, Optional
from uuid import uuid4

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
from app.models.interview import InterviewCaller, InterviewStage, InterviewStageEvent, InterviewStatus
from app.interview_normalize import normalize_stage, normalize_status, pending_question_updates, translate_legacy_write
from app.interview_caller import (
    ENDED_STATUSES,
    CallerError,
    CallerIn,
    build_round_caller,
    round_caller_snapshot,
)
from app.routers._util import doc_to_dict, parse_date, populate_owner, to_object_id
from app.slack_bot import sync_caller_thread

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
    """Map legacy stage keys (tech, tech1, hiring_manager, …) to current values."""
    return normalize_stage(_stage_value(stage))


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


class RoundIn(BaseModel):
    """One interview round (stage + when + outcome + per-round details)."""

    stage: str
    scheduledAt: str
    endsAt: Optional[str] = None
    status: Optional[str] = "scheduled"
    interviewerName: Optional[str] = None
    note: Optional[str] = ""
    transcript: Optional[str] = ""
    caller: Optional[CallerIn] = None


class InterviewCreate(BaseModel):
    accountId: str
    companyName: Optional[str] = None
    appliedPosition: Optional[str] = None
    jobUrl: Optional[str] = None
    mainTechStack: Optional[str] = None
    round: Optional[RoundIn] = None
    # Legacy flat shape (older clients): mapped into `round` when it is absent.
    scheduledAt: Optional[str] = None
    endsAt: Optional[str] = None
    stage: Optional[str] = None
    status: Optional[str] = None
    interviewerName: Optional[str] = None
    transcript: Optional[str] = ""
    note: Optional[str] = ""
    caller: Optional[CallerIn] = None


class StageCreate(RoundIn):
    # Add next round: optionally mark the previous (scheduled/completed) round passed.
    markPreviousPassed: bool = False


class StagePatch(BaseModel):
    stage: Optional[str] = None
    scheduledAt: Optional[str] = None
    endsAt: Optional[str] = None
    status: Optional[str] = None
    interviewerName: Optional[str] = None
    transcript: Optional[str] = None
    note: Optional[str] = None
    caller: Optional[CallerIn] = None
    # Caller rounds only; owner or admin.
    confirmed: Optional[bool] = None


class InterviewUpdate(BaseModel):
    """Application-level fields. Rounds are edited through /{id}/stages."""

    accountId: Optional[str] = None
    companyName: Optional[str] = None
    appliedPosition: Optional[str] = None
    jobUrl: Optional[str] = None
    mainTechStack: Optional[str] = None
    # Legacy board/quick-status path (stale tabs and the Canceled column).
    stage: Optional[str] = None
    status: Optional[str] = None
    scheduledAt: Optional[str] = None
    endsAt: Optional[str] = None
    # Whole-history rewrites are no longer accepted (they silently deleted rounds).
    stageHistory: Optional[Any] = None


def _round_caller(
    body: CallerIn,
    *,
    starts_at: datetime,
    previous: Optional[InterviewCaller],
) -> InterviewCaller:
    try:
        return build_round_caller(body, starts_at=starts_at, previous=previous)
    except CallerError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))


def mark_previous_passed(history: list[InterviewStageEvent]) -> bool:
    """Before appending a round: a still-open previous round becomes passed."""
    if not history:
        return False
    tip = history[-1]
    if tip.status in (InterviewStatus.scheduled, InterviewStatus.completed):
        tip.status = InterviewStatus.passed
        return True
    return False


def round_ends_at(start: datetime, ends: Optional[datetime]) -> datetime:
    """Round end: `ends` when it is after `start`, else start + 1 hour."""
    if ends is not None and _utc_naive(ends) > _utc_naive(start):
        return ends
    return start + timedelta(hours=1)


def _carry_status(current: Optional[InterviewStatus], *, appended: bool) -> Optional[InterviewStatus]:
    """Status for the tip after an update: a newly appended round starts Scheduled."""
    return InterviewStatus.scheduled if appended else current


async def _save_interview(iv: Interview) -> None:
    """Save, then re-link questions owed by a document resolved on read.

    An unmigrated interview loses its "rejected" round when it is saved
    (the model resolves it on load); its extracted questions must follow to
    the round before, or they are orphaned and the later migration can no
    longer find them.
    """
    await iv.save()
    if not iv.needs_question_sync:
        return
    remap = iv.pending_question_remap
    coll = InterviewQuestion.get_motor_collection()
    questions = await coll.find(
        {"interviewId": iv.id, "$or": [{"stageId": {"$in": list(remap)}}, {"stage": "rejected"}]},
        {"interviewId": 1, "stageId": 1, "stage": 1},
    ).to_list(None)
    for qid, payload in pending_question_updates(iv, questions):
        await coll.update_one({"_id": qid}, {"$set": payload})
    iv.clear_question_sync()


def _round_snapshots(iv: Interview) -> dict[str, Optional[dict]]:
    return {e.id: round_caller_snapshot(e) for e in iv.stageHistory if e.id}


async def _sync_caller_posts(
    iv: Interview,
    before: dict[str, Optional[dict]],
    *,
    added: Iterable[str] = (),
    deleted: Iterable[str] = (),
) -> None:
    """Bring the interview's #caller thread in line with a write (snapshots in `before` were taken
    before it). Rounds being deleted are still in iv.stageHistory; their caller saves the interview."""
    gone = set(deleted)
    if await sync_caller_thread(iv, before, added=added, deleted=gone) and not gone:
        await _save_interview(iv)


def _can_view(iv: Interview, user: UserExtra) -> bool:
    """Admin, interview manager, or owner."""
    return user.role in ("admin", "interview_manager") or str(iv.createdBy) == str(user.id)


def _parse_datetime(s: str) -> Optional[datetime]:
    if not s:
        return None
    try:
        if len(s) == 10 and s[4] == "-" and s[7] == "-":
            return datetime.fromisoformat(f"{s}T12:00:00")
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _utc_naive(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None or dt.tzinfo is None:
        return dt
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def _tip(iv: Interview) -> Optional[InterviewStageEvent]:
    return iv.stageHistory[-1] if iv.stageHistory else None


def _find_stage(iv: Interview, stage_id: str) -> int:
    for idx, e in enumerate(iv.stageHistory):
        if e.id == stage_id:
            return idx
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="stage not found")


def migrate_legacy_stage_fields(iv: Interview) -> bool:
    """Move interview-level transcript/note/status onto the tip stage.

    Seeds stageHistory from the current stage when it is empty. Idempotent:
    top-level transcript/note are cleared once moved. Returns True if changed.
    """
    changed = False
    if not iv.stageHistory and iv.stage is not None:
        iv.stageHistory.append(
            InterviewStageEvent(
                stage=_stage_value(iv.stage) or "",
                at=iv.createdAt or datetime.now(timezone.utc),
                by=iv.createdBy,
                source="seed",
                scheduledAt=iv.scheduledAt,
                status=iv.status,
            )
        )
        changed = True
    tip = _tip(iv)
    if tip is None:
        return changed
    if tip.status is None and iv.status is not None:
        tip.status = iv.status
        changed = True
    if (iv.transcript or "").strip():
        if not (tip.transcript or "").strip():
            tip.transcript = iv.transcript
        iv.transcript = ""
        changed = True
    if (iv.note or "").strip():
        if not (tip.note or "").strip():
            tip.note = iv.note
        iv.note = ""
        changed = True
    return changed


def _status_value(value) -> Optional[str]:
    return getattr(value, "value", value)


def _sync_top_from_tip(iv: Interview) -> None:
    """Interview-level stage/status/scheduledAt always mirror the latest stage."""
    tip = _tip(iv)
    if tip is None:
        return
    s = _normalize_stage(tip.stage)
    iv.stage = InterviewStage(s) if s in STAGES else None
    iv.status = tip.status
    if tip.scheduledAt is not None:
        cur = _utc_naive(iv.scheduledAt)
        new = _utc_naive(tip.scheduledAt)
        # Timed rounds (they have an end) set the exact start; legacy date-only
        # rounds only move the interview's date and keep its time.
        if tip.endsAt is not None or cur is None or cur.date() != new.date():
            iv.scheduledAt = tip.scheduledAt
    if tip.endsAt is not None:
        iv.endsAt = tip.endsAt
    start = _utc_naive(iv.scheduledAt)
    ends = _utc_naive(iv.endsAt)
    if start is not None and (ends is None or ends <= start):
        iv.endsAt = start + timedelta(hours=1)
    # Interviewer and caller live on rounds; the interview-level fields mirror
    # the latest round for one release (DM digest, _can_view read them).
    iv.interviewerName = tip.interviewerName
    iv.caller = tip.caller
    iv.confirmed = bool(getattr(tip, "confirmed", False))


def _reject_adjacent_duplicate(iv: Interview) -> None:
    for a, b in zip(iv.stageHistory, iv.stageHistory[1:]):
        if _normalize_stage(a.stage) == _normalize_stage(b.stage):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"{a.stage} cannot directly follow itself",
            )


async def _load_editable(iv_id: str, user: UserExtra) -> Interview:
    iv = await Interview.get(to_object_id(iv_id))
    if iv is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    if user.role != "admin" and str(iv.createdBy) != str(user.id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")
    return iv


async def _after_save(
    iv: Interview,
    *,
    user: UserExtra,
    extract_stage_ids: Iterable[Optional[str]] = (),
) -> None:
    """Pipeline re-sync and transcript extraction after an update."""
    try:
        from app.routers.applications import attach_interview_to_app
        await attach_interview_to_app(user_id=user.id, interview=iv)
    except Exception as exc:  # noqa: BLE001
        log.warning("pipeline auto-link failed for interview %s on update: %s", iv.id, exc)

    for sid in extract_stage_ids:
        asyncio.create_task(extract_and_score(iv, sid))


async def _interview_out(iv: Interview) -> dict:
    rows = await populate_owner([iv], field="createdBy")
    out = await _populate_account(rows)
    return out[0]


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
            item[field] = {
                "_id": str(acc.id),
                "name": acc.name,
                "country": acc.country,
                "region": acc.region,
                "email": "",
            }
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


def range_match(from_dt: Optional[datetime], to_dt: Optional[datetime]) -> dict:
    """Interviews with any round dated in range (or a dated interview with no rounds)."""
    rng: dict = {}
    if from_dt is not None:
        rng["$gte"] = from_dt
    if to_dt is not None:
        rng["$lte"] = to_dt
    if not rng:
        return {}
    return {"$or": [
        {"stageHistory": {"$elemMatch": {"scheduledAt": rng}}},
        {"stageHistory": {"$size": 0}, "scheduledAt": rng},
    ]}


def _as_naive_utc(value: Any) -> Optional[datetime]:
    if isinstance(value, str):
        value = _parse_datetime(value)
    if not isinstance(value, datetime):
        return None
    return _utc_naive(value)


def _iso_utc(value: Any) -> Optional[str]:
    dt = _as_naive_utc(value)
    return dt.isoformat() + "Z" if dt is not None else None


def flatten_rounds(
    interviews: list[dict],
    *,
    from_dt: datetime,
    to_dt: datetime,
    stage: str = "",
    status: str = "",
) -> list[dict]:
    """Calendar rows: one per round dated in [from_dt, to_dt), filtered on the round itself."""
    lo, hi = _utc_naive(from_dt), _utc_naive(to_dt)
    want_stage = _normalize_stage(stage) if stage else None
    rows = []
    for iv in interviews:
        account = iv.get("accountId") if isinstance(iv.get("accountId"), dict) else {}
        owner = iv.get("createdBy")
        owner_id = str(owner.get("_id")) if isinstance(owner, dict) else str(owner or "")
        for e in iv.get("stageHistory") or []:
            start = _as_naive_utc(e.get("scheduledAt"))
            if start is None or not (lo <= start < hi):
                continue
            if want_stage and _normalize_stage(e.get("stage")) != want_stage:
                continue
            if status and normalize_status(e.get("status")) != status:
                continue
            caller = e.get("caller") or {}
            rows.append({
                "interviewId": str(iv.get("_id")),
                "roundId": e.get("id"),
                "stage": _normalize_stage(e.get("stage")),
                "status": normalize_status(e.get("status")),
                "scheduledAt": _iso_utc(start),
                "endsAt": _iso_utc(e.get("endsAt")),
                "companyName": iv.get("companyName"),
                "profileLabel": (account.get("name") or account.get("email") or "").strip() or None,
                "ownerId": owner_id,
                "ownerName": iv.get("ownerName") or iv.get("ownerEmail"),
                "hasCaller": bool(caller.get("enabled")),
            })
    rows.sort(key=lambda r: r["scheduledAt"])
    return rows


_SORT_FIELDS = {"latest": "scheduledAt", "company": "companyName", "stage": "stage", "status": "status"}


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
    sort: str = Query("latest"),
    dir: str = Query("desc"),
) -> dict:
    # Every authenticated user can list/filter all interviews. Pass creatorId
    # to narrow to one owner; omit it to see everyone.
    query: dict[str, Any] = range_match(parse_date(from_), parse_date(to, end_of_day=True))

    # Stage and status describe the latest round (mirrored on the interview).
    if stage:
        query["stage"] = {"$in": _stage_filter_values(stage)}
    if accountId:
        query["accountId"] = to_object_id(accountId)
    if status_:
        if status_ not in STATUSES:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid status")
        query["status"] = status_
    if creatorId:
        query["createdBy"] = to_object_id(creatorId)

    # Legacy clients send sort=asc|desc (by date).
    if sort in ("asc", "desc"):
        sort, dir = "latest", sort
    field = _SORT_FIELDS.get(sort, "scheduledAt")
    direction = 1 if dir == "asc" else -1
    skip = (page - 1) * limit
    total = await Interview.find(query).count()
    # Case-insensitive ordering ("acme" next to "Acme", not after "Zeta").
    rows = await Interview.find(query, collation={"locale": "en", "strength": 2}).sort(
        [(field, direction), ("_id", direction)]
    ).skip(skip).limit(limit).to_list()
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


@router.get("/rounds")
async def list_interview_rounds(
    user: Annotated[UserExtra, Depends(get_current_user)],
    from_: str = Query("", alias="from"),
    to: str = Query(""),
    accountId: str = Query(""),
    stage: str = Query(""),
    status_: str = Query("", alias="status"),
    creatorId: str = Query(""),
) -> dict:
    """Calendar feed: every round dated in [from, to), flattened. Same visibility as the list."""
    f_dt, t_dt = parse_date(from_), parse_date(to)
    if f_dt is None or t_dt is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="from and to are required")
    if not (timedelta(0) < _utc_naive(t_dt) - _utc_naive(f_dt) <= timedelta(days=45)):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="range must be 1 to 45 days")
    if status_ and status_ not in STATUSES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid status")
    query: dict[str, Any] = {"stageHistory": {"$elemMatch": {"scheduledAt": {"$gte": f_dt, "$lt": t_dt}}}}
    if accountId:
        query["accountId"] = to_object_id(accountId)
    if creatorId:
        query["createdBy"] = to_object_id(creatorId)
    rows = await Interview.find(query).to_list()
    out = await _populate_account(await populate_owner(rows, field="createdBy"))
    rounds = flatten_rounds(out, from_dt=f_dt, to_dt=t_dt, stage=stage, status=status_)
    # The interviews behind the rounds, so a click can open the side panel
    # (GET /{id} is limited to owner/admin/interview manager; this feed is not).
    shown = {r["interviewId"] for r in rounds}
    return {"rounds": rounds, "interviews": {str(i["_id"]): i for i in out if str(i["_id"]) in shown}}


@router.get("/{iv_id}")
async def get_interview(
    iv_id: str,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    oid = to_object_id(iv_id)
    iv = await Interview.get(oid)
    if iv is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    if not _can_view(iv, user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")
    if migrate_legacy_stage_fields(iv):
        await _save_interview(iv)
        tip = _tip(iv)
        if tip is not None:
            await InterviewQuestion.find(
                {"interviewId": iv.id, "stageId": None}
            ).update({"$set": {"stageId": tip.id}})
    return await _interview_out(iv)


def _build_round(
    rnd: RoundIn,
    *,
    user: UserExtra,
    source: str,
) -> InterviewStageEvent:
    """Validate a RoundIn and return the stage entry to store."""
    stage_raw, status_raw = translate_legacy_write(rnd.stage, rnd.status)
    stage_val = _coerce_write_stage(stage_raw or InterviewStage.intro.value)
    status_val = (status_raw or "").strip() or InterviewStatus.scheduled.value
    if status_val not in STATUSES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid status")
    start = _parse_datetime(rnd.scheduledAt)
    if start is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid scheduledAt")
    ends = None
    if rnd.endsAt:
        ends = _parse_datetime(rnd.endsAt)
        if ends is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid endsAt")
    entry = InterviewStageEvent(
        id=uuid4().hex,
        stage=stage_val,
        at=datetime.now(timezone.utc),
        by=user.id,
        source=source,
        scheduledAt=start,
        endsAt=round_ends_at(start, ends),
        status=InterviewStatus(status_val),
        interviewerName=(rnd.interviewerName or "").strip() or None,
        note=rnd.note or "",
        transcript=rnd.transcript or "",
    )
    if rnd.caller is not None:
        entry.caller = _round_caller(rnd.caller, starts_at=start, previous=None)
    return entry


def _legacy_round(body: InterviewCreate) -> RoundIn:
    """Older clients post the first round as flat interview fields."""
    return RoundIn(
        stage=body.stage or InterviewStage.intro.value,
        scheduledAt=body.scheduledAt or "",
        endsAt=body.endsAt,
        status=body.status or InterviewStatus.scheduled.value,
        interviewerName=body.interviewerName,
        note=body.note or "",
        transcript=body.transcript or "",
        caller=body.caller,
    )


async def _account_for_write(account_id: str, user: UserExtra) -> Any:
    account_oid = to_object_id(account_id)
    account = await Account.get(account_oid)
    if account is None:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid accountId")
    if user.role != "admin" and str(account.createdBy) != str(user.id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="cannot use an account you do not own")
    return account_oid


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_interview(
    body: InterviewCreate,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    company = (body.companyName or "").strip()
    if not company:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="companyName is required")
    account_oid = await _account_for_write(body.accountId, user)
    entry = _build_round(body.round or _legacy_round(body), user=user, source="create")

    # History is appended after construction: the model's read-time resolver
    # only understands raw dicts.
    iv = Interview(
        accountId=account_oid,
        createdBy=user.id,
        scheduledAt=entry.scheduledAt,
        endsAt=entry.endsAt,
        companyName=company,
        appliedPosition=(body.appliedPosition or "").strip() or None,
        jobUrl=(body.jobUrl or "").strip() or None,
        mainTechStack=(body.mainTechStack or "").strip() or None,
        stageHistory=[],
    )
    iv.stageHistory.append(entry)
    _sync_top_from_tip(iv)
    await iv.insert()

    if entry.transcript.strip():
        asyncio.create_task(extract_and_score(iv, entry.id))
    try:
        from app.routers.applications import attach_interview_to_app
        await attach_interview_to_app(user_id=user.id, interview=iv)
    except Exception as exc:  # noqa: BLE001
        log.warning("pipeline auto-link failed for interview %s: %s", iv.id, exc)
    await _sync_caller_posts(iv, {})
    return await _interview_out(iv)


@router.put("/{iv_id}")
async def update_interview(
    iv_id: str,
    body: InterviewUpdate,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    if body.stageHistory is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="edit rounds with /interviews/{id}/stages",
        )
    oid = to_object_id(iv_id)
    iv = await Interview.get(oid)
    if iv is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    is_admin = user.role == "admin"
    if not is_admin and str(iv.createdBy) != str(user.id):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")

    before = _round_snapshots(iv)
    prev_sched = iv.scheduledAt
    migrated = migrate_legacy_stage_fields(iv)

    # Stale clients still send stage "rejected": set the current round's
    # status instead, without a new round or a date change.
    legacy_rejected = normalize_stage(body.stage) == "rejected"
    body.stage, body.status = translate_legacy_write(body.stage, body.status)
    if legacy_rejected:
        body.scheduledAt = None
        body.endsAt = None

    if body.accountId is not None:
        iv.accountId = await _account_for_write(body.accountId, user)
    if body.companyName is not None:
        company = body.companyName.strip()
        if not company:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="companyName is required")
        iv.companyName = company
    if body.appliedPosition is not None:
        iv.appliedPosition = body.appliedPosition.strip() or None
    if body.jobUrl is not None:
        iv.jobUrl = body.jobUrl.strip() or None
    if body.mainTechStack is not None:
        iv.mainTechStack = body.mainTechStack.strip() or None

    # Legacy stage move (board drag from a stale tab): append / re-date a round.
    new_sched = iv.scheduledAt
    if body.scheduledAt is not None:
        sched = _parse_datetime(body.scheduledAt)
        if sched is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid scheduledAt")
        new_sched = sched
        tip = _tip(iv)
        if tip is not None and tip.scheduledAt is None and prev_sched is not None:
            tip.scheduledAt = prev_sched
    iv.scheduledAt = new_sched
    rounds_before = len(iv.stageHistory)
    if body.stage:
        stage_val = _coerce_write_stage(body.stage)
        prev = _stage_value(iv.stage)
        if stage_val != _normalize_stage(prev) or body.scheduledAt is not None:
            _append_stage_history(
                iv, stage_val, by=user.id, source="manual",
                previous=prev, previous_scheduled_at=prev_sched,
            )
    iv.status = _carry_status(iv.status, appended=len(iv.stageHistory) > rounds_before)
    if body.status is not None:
        if body.status == "":
            iv.status = None
        elif body.status not in STATUSES:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid status")
        else:
            iv.status = InterviewStatus(body.status)
    tip = _tip(iv)
    if tip is not None:
        tip.status = iv.status
        if _status_value(tip.status) in ENDED_STATUSES:
            tip.confirmed = False
        if tip.caller is not None and tip.caller.enabled and tip.scheduledAt is not None:
            tip.caller.startsAt = tip.scheduledAt
    _sync_top_from_tip(iv)
    await _save_interview(iv)

    if migrated and tip is not None:
        await InterviewQuestion.find(
            {"interviewId": iv.id, "stageId": None}
        ).update({"$set": {"stageId": tip.id}})
    await _sync_caller_posts(iv, before)
    await _after_save(iv, user=user)
    return await _interview_out(iv)


@router.post("/{iv_id}/stages", status_code=status.HTTP_201_CREATED)
async def add_interview_stage(
    iv_id: str,
    body: StageCreate,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    iv = await _load_editable(iv_id, user)
    before = _round_snapshots(iv)
    migrate_legacy_stage_fields(iv)
    if normalize_stage(body.stage) == "rejected":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="rejected is a status; set status on the current round",
        )
    entry = _build_round(body, user=user, source="stage")
    prev_tip = _tip(iv)
    if prev_tip is not None and _normalize_stage(prev_tip.stage) == entry.stage:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"{entry.stage} cannot directly follow itself",
        )
    if body.markPreviousPassed:
        mark_previous_passed(iv.stageHistory)
    iv.stageHistory.append(entry)
    _sync_top_from_tip(iv)
    await _save_interview(iv)

    await _sync_caller_posts(iv, before, added={entry.id})
    await _after_save(iv, user=user, extract_stage_ids=[entry.id] if entry.transcript.strip() else [])
    return await _interview_out(iv)


@router.patch("/{iv_id}/stages/{stage_id}")
async def update_interview_stage(
    iv_id: str,
    stage_id: str,
    body: StagePatch,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    iv = await _load_editable(iv_id, user)
    before = _round_snapshots(iv)
    migrate_legacy_stage_fields(iv)
    idx = _find_stage(iv, stage_id)
    entry = iv.stageHistory[idx]

    body.stage, body.status = translate_legacy_write(body.stage, body.status)
    if body.stage:
        entry.stage = _coerce_write_stage(body.stage)
    if body.scheduledAt is not None:
        sched = _parse_datetime(body.scheduledAt)
        if sched is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid scheduledAt")
        old_start, old_end = entry.scheduledAt, entry.endsAt
        if old_start is None or _utc_naive(old_start) != _utc_naive(sched):
            entry.confirmed = False  # a new time needs the caller lined up again
        entry.scheduledAt = sched
        # Rescheduling keeps the round's duration unless a new end is sent.
        if body.endsAt is None and old_start is not None and old_end is not None:
            entry.endsAt = round_ends_at(sched, sched + (_utc_naive(old_end) - _utc_naive(old_start)))
        if entry.caller is not None and entry.caller.enabled:
            entry.caller.startsAt = sched
    if body.endsAt is not None:
        ends = _parse_datetime(body.endsAt) if body.endsAt else None
        if body.endsAt and ends is None:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid endsAt")
        entry.endsAt = round_ends_at(entry.scheduledAt, ends)
    if body.status is not None:
        if body.status == "":
            entry.status = None
        elif body.status not in STATUSES:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="invalid status")
        else:
            entry.status = InterviewStatus(body.status)
    if body.interviewerName is not None:
        entry.interviewerName = body.interviewerName.strip() or None
    transcript_changed = body.transcript is not None and body.transcript != entry.transcript
    if body.transcript is not None:
        entry.transcript = body.transcript
    if body.note is not None:
        entry.note = body.note
    if body.caller is not None:
        entry.caller = _round_caller(body.caller, starts_at=entry.scheduledAt, previous=entry.caller)
    ended = _status_value(entry.status) in ENDED_STATUSES
    if entry.caller is None or not entry.caller.enabled or ended:
        entry.confirmed = False  # a caller is only lined up for an active caller round
    if body.confirmed is not None:
        if body.confirmed and (entry.caller is None or not entry.caller.enabled):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Only caller rounds are confirmed")
        if body.confirmed and ended:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST, detail="A canceled or rejected round can't be confirmed",
            )
        entry.confirmed = body.confirmed
    iv.stageHistory[idx] = entry

    _reject_adjacent_duplicate(iv)
    _sync_top_from_tip(iv)
    await _save_interview(iv)

    extract_ids: list[Optional[str]] = []
    if transcript_changed:
        if entry.transcript.strip():
            extract_ids.append(entry.id)
        else:
            await InterviewQuestion.find({"interviewId": iv.id, "stageId": entry.id}).delete()
    await _sync_caller_posts(iv, before)
    await _after_save(iv, user=user, extract_stage_ids=extract_ids)
    return await _interview_out(iv)


@router.delete("/{iv_id}/stages/{stage_id}")
async def delete_interview_stage(
    iv_id: str,
    stage_id: str,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    iv = await _load_editable(iv_id, user)
    before = _round_snapshots(iv)
    migrate_legacy_stage_fields(iv)
    idx = _find_stage(iv, stage_id)
    if len(iv.stageHistory) <= 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="An interview needs at least one round",
        )
    # Remove the round's #caller post while the round still exists.
    await _sync_caller_posts(iv, before, deleted={stage_id})
    iv.stageHistory.pop(idx)
    _reject_adjacent_duplicate(iv)
    _sync_top_from_tip(iv)
    await _save_interview(iv)
    await InterviewQuestion.find({"interviewId": iv.id, "stageId": stage_id}).delete()
    await _after_save(iv, user=user)
    return await _interview_out(iv)


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

    before = _round_snapshots(iv)
    await _sync_caller_posts(iv, before, deleted=set(before))
    await iv.delete()
    await InterviewQuestion.find({"interviewId": iv.id}).delete()
    return {"ok": True}


# ---------- Extracted questions ---------------------------------------------


@router.get("/{iv_id}/questions")
async def list_interview_questions(
    iv_id: str,
    user: Annotated[UserExtra, Depends(get_current_user)],
    stage_id: Optional[str] = Query(None, alias="stageId"),
) -> dict:
    oid = to_object_id(iv_id)
    iv = await Interview.get(oid)
    if iv is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="not found")
    if not _can_view(iv, user):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="forbidden")
    tip = _tip(iv)
    sid = stage_id or (tip.id if tip else None)
    query: dict[str, Any] = {"interviewId": oid, "stageId": sid}
    if tip is not None and sid == tip.id:
        # Questions extracted before stages had ids belong to the current round.
        query = {"interviewId": oid, "stageId": {"$in": [sid, None]}}
    rows = (
        await InterviewQuestion.find(query)
        .sort([("createdAt", 1)])
        .to_list()
    )
    return {"questions": [doc_to_dict(q) for q in rows]}


@router.post("/{iv_id}/extract")
async def reextract_interview(
    iv_id: str,
    user: Annotated[UserExtra, Depends(get_current_user)],
    stage_id: Optional[str] = Query(None, alias="stageId"),
) -> dict:
    """Force re-extraction. Useful after fixing transcript or for legacy rows
    that never went through the auto-trigger."""
    iv = await _load_editable(iv_id, user)
    if iv.stageHistory:
        entry = iv.stageHistory[_find_stage(iv, stage_id)] if stage_id else iv.stageHistory[-1]
        transcript, sid = entry.transcript, entry.id
    else:
        transcript, sid = iv.transcript, None
    if not (transcript or "").strip():
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No transcript to extract")
    asyncio.create_task(extract_and_score(iv, sid))
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
            f"Filter context:\n{json.dumps(filter_context, ensure_ascii=False, separators=(',', ':'))}\n\n"
            f"Per-stage digest:\n{json.dumps(digest, ensure_ascii=False, separators=(',', ':'))}\n\n"
            f"Recent AI follow-up questions the candidate asked (uncertainty signal):\n"
            f"{json.dumps(follow_up_digest, ensure_ascii=False, separators=(',', ':'))}"
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
    _json_sep = (",", ":")
    grounding = (
        "Filter the user is viewing:\n"
        f"{json.dumps(body.filterContext or {}, ensure_ascii=False, separators=_json_sep)}\n\n"
        "Analysis result the user is viewing:\n"
        f"{json.dumps(body.result or {}, ensure_ascii=False, separators=_json_sep)}"
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
