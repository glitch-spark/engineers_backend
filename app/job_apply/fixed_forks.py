"""Fixed-resume forks: when scoring picks a profile's uploaded ("fixed") resume for a job (its best, at or above the
run's minimum), that pick becomes a per-job record like a tailored resume: a ResumeApplication with `fixedFrom`, its
own copy of the file, the job's description and the resume's score with it. It sits in the job's tailored slot
(row.tailored[accountId]), so download, sheet export and mark-applied treat it as the job's resume, and the Generated
page, the interview company search and statistics see it.

Spec: docs/superpowers/specs/2026-10-24-fixed-resume-forks-design.md
"""
import asyncio
import logging
import re
from datetime import datetime
from typing import Optional
from urllib.parse import urlsplit

from beanie import PydanticObjectId

from app.config import get_settings

log = logging.getLogger(__name__)

_UNSAFE = re.compile(r"[^\w .()-]+")


def _open_profiles(row) -> set[str]:
    """Profiles that pass the job's checks (as Job Applies' table shows them)."""
    return {pg.accountId for pg in row.profileGates if not any(g.result == "fail" for g in pg.gates)}


def _company(row) -> str:
    """As on a tailored resume of the job (routers.job_applies._company_label)."""
    return row.company or (urlsplit(row.url or "").hostname or "").removeprefix("www.") or row.title or "company"


def is_forkable(resume) -> bool:
    """Only a stored PDF is forked: downloads name the file Resume.pdf. Text-only resumes keep the shared file."""
    pdf = (resume.fileType or "") == "application/pdf" or (resume.filename or "").lower().endswith(".pdf")
    return bool(resume.s3Key) and pdf


def fork_key(account_id: str, fork_id: str, filename: str) -> str:
    return f"job-applies/fixed/{account_id}/{fork_id}/{_UNSAFE.sub('_', filename or 'Resume.pdf')[:120]}"


async def copy_file(src: str, dst: str) -> bool:
    """Server-side S3 copy of the profile resume's file; False without S3."""
    from app.resume_service import _s3_client, s3_enabled

    if not s3_enabled():
        return False
    bucket = get_settings().AWS_S3_BUCKET
    await asyncio.to_thread(
        _s3_client().copy_object,
        Bucket=bucket, Key=dst, CopySource={"Bucket": bucket, "Key": src}, ServerSideEncryption="AES256",
    )
    return True


async def _in_use(fork_id: PydanticObjectId, row, acc: str) -> bool:
    """Applied with (this run or the permanent log), exported to a sheet, or linked to an interview."""
    from app.models import Interview
    from app.models.job_apply import JobApplicationLog

    fid = str(fork_id)
    if any(m.tailoredJobId == fid for m in row.appliedResumes) or acc in (row.exported or {}):
        return True
    if await JobApplicationLog.get_motor_collection().count_documents({"tailoredJobId": fid}, limit=1):
        return True
    return bool(await Interview.get_motor_collection().count_documents({"resumeLink.resumeJobId": fork_id}, limit=1))


async def _delete(fork_id: PydanticObjectId, s3_key: Optional[str]) -> None:
    from app import resume_files
    from app.models import ResumeApplication

    await ResumeApplication.get_motor_collection().delete_one({"_id": fork_id, "fixedFrom": {"$ne": None}})
    if s3_key:
        await resume_files.delete_resume_files([s3_key])


async def _retire(fork: dict, row, acc: str) -> bool:
    """Delete a fork nobody used; True when deleted. One in use is kept (as history, or in its slot)."""
    if await _in_use(fork["_id"], row, acc):
        return False
    await _delete(fork["_id"], fork.get("s3Key"))
    return True


async def _create(row, account, resume, match: dict) -> Optional[PydanticObjectId]:
    from app.models import ResumeApplication

    fork_id = PydanticObjectId()
    key = fork_key(str(account.id), str(fork_id), resume.filename)
    try:
        if not await copy_file(resume.s3Key, key):
            return None
    except Exception as exc:  # noqa: BLE001 — no fork: the job keeps the shared file, as before forks
        log.warning("copying fixed resume %s for row %s failed: %s", resume.id, row.id, exc)
        return None
    now = datetime.utcnow()
    company = _company(row)
    await ResumeApplication(
        id=fork_id, userId=row.userId, accountId=account.id, profileName=account.name, companyName=company,
        companyNameLower=company.lower(), jobDescription=row.jdText or "", jobUrl=row.url, status="completed",
        step="done", startedAt=now, completedAt=now, executionMs=0, source="job_applies", countsAsBid=False,
        jobApplyRowId=row.id, fixedFrom=str(resume.id), pdfFilename=resume.filename, s3Key=key,
        plainText=resume.markdown, matchScore=match,
    ).insert()
    return fork_id


async def sync_row(row, threshold: int, accounts: Optional[dict] = None, *, dry_run: bool = False) -> int:
    """Bring the row's fixed forks in line with its scores (after scoring, or when the run's minimum changes):
    a real tailored resume in the slot is left alone; a qualifying best uploaded resume gets a fork (the same one
    refreshed, another one replaced); otherwise a fork in the slot is retired. `accounts`: profiles already loaded
    (accountId → Account), shared across a run's rows. Returns the forks created (`dry_run`: would create; nothing
    is written)."""
    from app.job_apply import tailored_score
    from app.models import Account, ResumeApplication
    from app.models.job_apply import JobApplyRow

    accounts = {} if accounts is None else accounts
    jobs = ResumeApplication.get_motor_collection()
    rows = JobApplyRow.get_motor_collection()
    best: dict = {}
    for s in row.bestByProfile:
        best.setdefault(s.accountId, s)
    open_profiles = _open_profiles(row)
    created = 0
    for acc in sorted(set(best) | set(row.tailored)):
        slot = row.tailored.get(acc)
        current = await jobs.find_one({"_id": slot}, {"fixedFrom": 1, "s3Key": 1, "matchScore": 1}) if slot else None
        if current is not None and not current.get("fixedFrom"):
            continue  # a tailored resume: it wins
        s = best.get(acc)
        account = resume = None
        if s is not None and s.total >= threshold and acc in open_profiles:
            if acc not in accounts and PydanticObjectId.is_valid(acc):
                accounts[acc] = await Account.get(PydanticObjectId(acc))
            account = accounts.get(acc)
            resume = next((r for r in (account.resumes if account else []) if str(r.id) == s.resumeId), None)
        if resume is None or not is_forkable(resume):
            if current is not None and not dry_run and await _retire(current, row, acc):
                await rows.update_one({"_id": row.id, f"tailored.{acc}": slot}, {"$unset": {f"tailored.{acc}": ""}})
                row.tailored.pop(acc, None)
            continue
        match = tailored_score.match_from_score(s).model_dump()
        if current is not None and current.get("fixedFrom") == s.resumeId:
            if current.get("matchScore") != match and not dry_run:
                await jobs.update_one({"_id": slot}, {"$set": {"matchScore": match, "updatedAt": datetime.utcnow()}})
            continue
        if dry_run:
            created += 1
            continue
        fork_id = await _create(row, account, resume, match)
        if fork_id is None:
            continue
        # Compare-and-set, as tailoring does: a tailor that took the slot meanwhile wins and ours is dropped.
        linked = await rows.update_one(
            {"_id": row.id, f"tailored.{acc}": slot},
            {"$set": {f"tailored.{acc}": fork_id, "updatedAt": datetime.utcnow()}},
        )
        if not linked.modified_count:
            await _delete(fork_id, fork_key(str(account.id), str(fork_id), resume.filename))
            continue
        row.tailored[acc] = fork_id
        created += 1
        if current is not None:
            await _retire(current, row, acc)  # the previous pick: kept as history when in use
    return created


async def sync_rows(run, rows: list, *, dry_run: bool = False) -> int:
    """sync_row for each scored row of `run`; one row's failure doesn't stop the rest."""
    from app.job_apply import jd_store

    await jd_store.hydrate(rows)
    accounts: dict = {}
    created = 0
    for row in rows:
        if not row.scoredAt:
            continue
        try:
            created += await sync_row(row, run.threshold, accounts, dry_run=dry_run)
        except Exception:  # noqa: BLE001
            log.exception("fixed forks for row %s failed", row.id)
    return created
