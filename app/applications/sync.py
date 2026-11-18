"""Writes `applications` rows: a daily full rebuild and a cheap incremental pass, both run from the cron tick.

Incremental approach (deliberately simple): the accounts touched since the checkpoint are rebuilt whole (all their
rows since BACKFILL_SINCE), not just the touched keys. Touched = accounts of changed bids / resumes / interviews, of
the rows a changed interview used to point at (its old match) and of the resume it links to now (its new match).
Rebuilding whole accounts also re-matches every interview that could land on those rows and drops rows whose
source is gone. Rows are replaced as whole documents, so a row that loses its interview match loses its fields;
rows whose content did not change are not written. A cross-profile resume link pulls the linked resume's account into
the rebuilt set, so an interview is matched exactly as in a full rebuild.

Deploy order: deploy with APPLICATIONS_SYNC_DISABLED=1, run `python -m scripts.build_applications --apply`, then
unset the flag. The tick bounds each pass to FULL_TIMEOUT; a timed-out full rebuild is retried after FULL_RETRY."""

import asyncio
import logging
import os
import time
from datetime import date, datetime, timedelta
from typing import Optional

from pymongo import UpdateOne

from app.applications.rows import _key, build_rows, match_interviews
from app.models import Account, Application, Bid, Interview, ResumeApplication

log = logging.getLogger("applications_sync")

BACKFILL_SINCE = date(2026, 8, 1)
FULL_TIMEOUT = 240  # seconds: the whole pass must fit inside one 5-minute cron run
FULL_RETRY = timedelta(hours=1)  # after a timed-out full rebuild
_SETTINGS_ID = "applications_sync"
_FIELDS = frozenset(Application.model_fields) - {"id", "revision_id", "createdAt"}
_VOLATILE = frozenset({"_id", "createdAt", "updatedAt", "syncedAt"})
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
    resume_coll, iv_coll = ResumeApplication.get_motor_collection(), Interview.get_motor_collection()
    resume_filter = {"createdAt": {"$gte": since_dt}, "countsAsBid": {"$ne": False}}
    dated = {"$or": [{"createdAt": {"$gte": since_dt}}, {"scheduledAt": {"$gte": since_dt}},
                     {"stageHistory.scheduledAt": {"$gte": since_dt}}]}
    accts = None if account_ids is None else set(account_ids)
    while True:  # scoped: grow the account set until every cross-profile resume link resolves as in a full rebuild
        scope = {} if accts is None else {"accountId": {"$in": list(accts)}}  # None in the list matches no profile
        resumes = await resume_coll.find({**scope, **resume_filter}, _proj(_RESUME_FIELDS)).to_list(None)
        iv_filter = dated if accts is None else {"$and": [dated, {"$or": [
            scope, {"resumeLink.resumeJobId": {"$in": [r["_id"] for r in resumes]}}]}]}  # a link can cross profiles
        iv_docs = await iv_coll.find(iv_filter, _proj(_IV_FIELDS)).to_list(None)
        if accts is None:
            break
        linked = {(d.get("resumeLink") or {}).get("resumeJobId") for d in iv_docs} - {None} - {r["_id"] for r in resumes}
        extra = {d.get("accountId") async for d in resume_coll.find(
            {"_id": {"$in": list(linked)}, **resume_filter}, {"accountId": 1})} - accts if linked else set()
        if not extra:
            break
        accts |= extra
    bids = await Bid.get_motor_collection().find(
        {**scope, "firstAt": {"$gte": since_dt}}, _proj(_BID_FIELDS)).to_list(None)
    ids = {d["accountId"] for d in bids + resumes if d.get("accountId")}
    accounts = {a["_id"]: {"region": a.get("region"), "createdAt": a.get("createdAt")}
                async for a in Account.get_motor_collection().find({"_id": {"$in": list(ids)}}, {"region": 1, "createdAt": 1})}
    rows = build_rows(bids, resumes, accounts)
    resume_row = {str(r["_id"]): _key(r.get("accountId"), r.get("jobUrl"), "resumeapplications", r["_id"])[0] for r in resumes}
    matched, unmatched = match_interviews(rows, [Interview.model_validate(d) for d in iv_docs], resume_row)

    coll = Application.get_motor_collection()
    existing = {d["key"]: d async for d in coll.find({**scope, "appliedAt": {"$gte": since_dt}})}
    gone = sorted(existing.keys() - set(rows))
    if apply:
        now = datetime.utcnow()
        # $set + $unset of every other field = a whole-document replace (mongomock's bulk ReplaceOne is unsupported)
        ops = []
        for k, r in rows.items():
            doc = {f: v for f, v in r.items() if v is not None}
            old = existing.get(k)
            if old and doc == {f: v for f, v in old.items() if f not in _VOLATILE}:
                continue  # unchanged: no write, so syncedAt/updatedAt stay and a no-change pass costs no writes
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
    changed = {"updatedAt": {"$gte": checkpoint}}  # indexed on all three sources; created docs start with updatedAt = createdAt
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
    last_full, checkpoint, attempt = state.get("lastFull"), state.get("checkpoint"), state.get("lastFullAttempt")
    due = last_full is None or checkpoint is None or start - last_full > timedelta(hours=24)
    full = due and (attempt is None or start - attempt > FULL_RETRY)
    if not full and checkpoint is None:
        return {"skipped": "waiting to retry the full rebuild"}
    stage = "full_rebuild" if full else "incremental"
    t0 = time.monotonic()
    try:
        out = await asyncio.wait_for(
            full_rebuild(BACKFILL_SINCE, apply=True) if full else incremental(checkpoint), FULL_TIMEOUT)
    except asyncio.TimeoutError:
        log.error("applications sync stage=%s timed out after %.0fs", stage, time.monotonic() - t0)
        if full:
            await settings.update_one({"_id": _SETTINGS_ID}, {"$set": {"lastFullAttempt": start}}, upsert=True)
        return {"error": "timeout"}  # lastFull and checkpoint stay: the next pass redoes what was cut short
    patch = {"checkpoint": start, **({"lastFull": start} if full else {})}
    await settings.update_one({"_id": _SETTINGS_ID}, {"$set": patch}, upsert=True)
    log.info("applications sync stage=%s %s in %.1fs", stage, out, time.monotonic() - t0)
    return out
