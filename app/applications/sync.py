"""Writes `applications` rows: a daily full rebuild and a cheap incremental pass, both run from the cron tick.

Incremental approach (deliberately simple): the accounts touched since the checkpoint are rebuilt whole (all their
rows since BACKFILL_SINCE), not just the touched keys. Touched = accounts of changed bids / resumes / interviews, of
the rows a changed interview used to point at (its old match) and of the resume it links to now (its new match).
Rebuilding whole accounts also re-matches every interview that could land on those rows and drops rows whose
source is gone. Rows are replaced as whole documents, so a row that loses its interview match loses its fields."""

import logging
import os
from datetime import date, datetime, timedelta
from typing import Optional

from pymongo import UpdateOne

from app.applications.rows import _key, build_rows, match_interviews
from app.models import Account, Application, Bid, Interview, ResumeApplication

log = logging.getLogger("applications_sync")

BACKFILL_SINCE = date(2026, 8, 1)
_SETTINGS_ID = "applications_sync"
_FIELDS = frozenset(Application.model_fields) - {"id", "revision_id", "createdAt"}
_BID_FIELDS = ("kind", "bidderId", "accountId", "ownerId", "jobKey", "jobUrl", "firstAt", "status")
_RESUME_FIELDS = ("userId", "accountId", "jobUrl", "createdAt", "countsAsBid", "matchScore", "promptId",
                  "promptName", "templateName", "jobDescription")
_IV_FIELDS = ("accountId", "createdBy", "scheduledAt", "stage", "status", "jobUrl", "stageHistory", "resumeLink",
              "createdAt", "updatedAt")


def _proj(fields) -> dict:
    return {f: 1 for f in fields}


async def _bulk_upsert(coll, ops: list[tuple[str, dict]]) -> None:
    """One bulk round trip of upserts by row key (a seam: mongomock cannot bulk_write)."""
    await coll.bulk_write([UpdateOne({"key": k}, u, upsert=True) for k, u in ops], ordered=False)


async def _sync(since: date, account_ids: Optional[list], *, apply: bool) -> dict:
    """Build and match the rows of `account_ids` (None = every account) from `since`; write them when `apply`."""
    since_dt = datetime(since.year, since.month, since.day)
    scope = {} if account_ids is None else {"accountId": {"$in": account_ids}}  # None in the list matches no profile
    bids = await Bid.get_motor_collection().find(
        {**scope, "firstAt": {"$gte": since_dt}}, _proj(_BID_FIELDS)).to_list(None)
    resumes = await ResumeApplication.get_motor_collection().find(
        {**scope, "createdAt": {"$gte": since_dt}, "countsAsBid": {"$ne": False}}, _proj(_RESUME_FIELDS)).to_list(None)
    ids = {d["accountId"] for d in bids + resumes if d.get("accountId")}
    accounts = {a["_id"]: {"region": a.get("region"), "createdAt": a.get("createdAt")}
                async for a in Account.get_motor_collection().find({"_id": {"$in": list(ids)}}, {"region": 1, "createdAt": 1})}
    rows = build_rows(bids, resumes, accounts)
    resume_row = {str(r["_id"]): _key(r.get("accountId"), r.get("jobUrl"), "resumeapplications", r["_id"])[0] for r in resumes}

    dated = {"$or": [{"createdAt": {"$gte": since_dt}}, {"scheduledAt": {"$gte": since_dt}},
                     {"stageHistory.scheduledAt": {"$gte": since_dt}}]}
    iv_filter = dated if account_ids is None else {"$and": [dated, {"$or": [
        scope, {"resumeLink.resumeJobId": {"$in": [r["_id"] for r in resumes]}}]}]}  # a link can cross profiles
    interviews = [Interview.model_validate(d) async for d in
                  Interview.get_motor_collection().find(iv_filter, _proj(_IV_FIELDS))]
    matched, unmatched = match_interviews(rows, interviews, resume_row)

    coll = Application.get_motor_collection()
    existing = {d["key"] async for d in coll.find({**scope, "appliedAt": {"$gte": since_dt}}, {"key": 1})}
    gone = sorted(existing - set(rows))
    if apply:
        now = datetime.utcnow()
        # $set + $unset of every other field = a whole-document replace (mongomock's bulk ReplaceOne is unsupported)
        ops = []
        for k, r in rows.items():
            doc = {f: v for f, v in r.items() if v is not None}
            doc.update(updatedAt=now, syncedAt=now)
            ops.append((k, {"$set": doc, "$setOnInsert": {"createdAt": now},
                            "$unset": {f: "" for f in _FIELDS - doc.keys()}}))
        if ops:
            await _bulk_upsert(coll, ops)
        if gone:
            await coll.delete_many({"key": {"$in": gone}})
    return {"rows": len(rows), "matched": matched, "unmatched": unmatched,
            "noLink": sum(k.startswith("src:") for k in rows), "deleted": len(gone)}


async def full_rebuild(since: date, *, apply: bool) -> dict:
    """Every row from `since`; rows (appliedAt >= since) whose key no longer exists are deleted. Counts returned."""
    return await _sync(since, None, apply=apply)


async def incremental(checkpoint: datetime) -> dict:
    """Rebuild the accounts touched since `checkpoint` (see module doc)."""
    changed = {"$or": [{"updatedAt": {"$gte": checkpoint}}, {"createdAt": {"$gte": checkpoint}}]}
    accounts: set = set()
    for model in (Bid, ResumeApplication):
        accounts |= {d.get("accountId") async for d in model.get_motor_collection().find(changed, {"accountId": 1})}
    ivs = [d async for d in Interview.get_motor_collection().find(changed, {"accountId": 1, "resumeLink.resumeJobId": 1})]
    accounts |= {d["accountId"] for d in ivs}
    if ivs:
        accounts |= {d.get("accountId") async for d in Application.get_motor_collection().find(
            {"interviewId": {"$in": [d["_id"] for d in ivs]}}, {"accountId": 1})}  # old matches
        linked = [d["resumeLink"]["resumeJobId"] for d in ivs if (d.get("resumeLink") or {}).get("resumeJobId")]
        if linked:
            accounts |= {d.get("accountId") async for d in ResumeApplication.get_motor_collection().find(
                {"_id": {"$in": linked}}, {"accountId": 1})}  # new matches
    if not accounts:
        return {"rows": 0, "matched": 0, "unmatched": 0, "noLink": 0, "deleted": 0, "incremental": True}
    return {**await _sync(BACKFILL_SINCE, list(accounts), apply=True), "incremental": True}


async def run_applications_tick(now: Optional[datetime] = None) -> dict:
    """Cron entry: a full rebuild when none ran in 24 h, else incremental from the last checkpoint."""
    if os.environ.get("APPLICATIONS_SYNC_DISABLED") == "1":
        return {"skipped": "disabled"}
    start = now or datetime.utcnow()
    settings = Application.get_motor_collection().database["app_settings"]
    state = await settings.find_one({"_id": _SETTINGS_ID}) or {}
    last_full, checkpoint = state.get("lastFull"), state.get("checkpoint")
    if last_full is None or checkpoint is None or start - last_full > timedelta(hours=24):
        out = await full_rebuild(BACKFILL_SINCE, apply=True)
        patch = {"checkpoint": start, "lastFull": start}
    else:
        out = await incremental(checkpoint)
        patch = {"checkpoint": start}
    await settings.update_one({"_id": _SETTINGS_ID}, {"$set": patch}, upsert=True)
    log.info("applications sync %s", out)
    return out
