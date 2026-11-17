"""Linking an interview to the resume and job it came from, for interview statistics.

Matches come from the profile's generated resumes (Resume Generator and Job Applies tailoring) and from Job Applies
applied-log entries made with an uploaded resume. Linking freezes a snapshot (JD, score, prompt, model) on the
interview: generated resumes lose their text to retention (app/resume_retention.py) and scores get filled in later,
and statistics must not move with them.

Spec: docs/superpowers/specs/2026-10-23-interview-resume-link-design.md
"""
import re
from datetime import datetime
from types import SimpleNamespace
from typing import Any, Literal, Optional

from beanie import PydanticObjectId
from fastapi import HTTPException, status
from pydantic import BaseModel

from app.models.interview import Interview, InterviewResumeLink, NoMatchReason
from app.models.resume_application import as_match

MAX_MATCHES = 20
SCAN_PER_SOURCE = 200  # candidates read from each source before the normalised-name filter
# Dropped when comparing company names: legal forms and a leading "the".
_NAME_NOISE = {
    "inc", "incorporated", "llc", "llp", "ltd", "limited", "corp", "corporation", "co", "company",
    "gmbh", "plc", "ag", "sa", "bv", "pty", "the",
}
_TAILORED_FIELDS = {
    "companyName": 1, "createdAt": 1, "pdfFilename": 1, "source": 1, "promptName": 1, "jobUrl": 1, "matchScore": 1,
    "fixedFrom": 1,
}


def normalize_company(name: Optional[str]) -> str:
    """"Acme, Inc." → "acme": lower case, punctuation and legal forms dropped (kept if that's all there is)."""
    words = re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).split()
    return " ".join([w for w in words if w not in _NAME_NOISE] or words)


def _iso(v: Optional[datetime]) -> Optional[str]:
    return v.isoformat().replace("+00:00", "") + "Z" if v else None


def _basename(path: Optional[str]) -> Optional[str]:
    return path.rsplit("/", 1)[-1] if path else None


def _tailored_out(d: dict) -> dict:
    return {
        "matchId": f"t:{d['_id']}", "kind": "fixed" if d.get("fixedFrom") else "tailored",
        "company": d.get("companyName") or "",
        "appliedAt": d.get("createdAt"), "filename": _basename(d.get("pdfFilename")),
        "source": d.get("source") or "generator", "promptName": d.get("promptName"), "jobUrl": d.get("jobUrl"),
        "score": as_match(d.get("matchScore")),
    }


def _uploaded_out(d: dict) -> dict:
    return {
        "matchId": f"u:{d['_id']}", "kind": "uploaded", "company": d.get("company") or "",
        "appliedAt": d.get("appliedAt"), "filename": d.get("filename") or None, "source": "job_applies",
        "promptName": None, "jobUrl": d.get("url"), "score": d.get("score"),
    }


async def _candidates(account_oid: PydanticObjectId, name_filter: Optional[dict] = None) -> tuple[list, list]:
    """The profile's completed generated resumes and uploaded-resume applied-log entries, newest first."""
    from app.models import ResumeApplication
    from app.models.job_apply import JobApplicationLog

    tq: dict[str, Any] = {"accountId": account_oid, "status": "completed"}
    uq: dict[str, Any] = {"accountId": str(account_oid), "tailoredJobId": None, "resumeId": {"$ne": None}}
    if name_filter:
        tq["companyName"], uq["company"] = name_filter, name_filter
    tailored = await ResumeApplication.get_motor_collection().find(tq, _TAILORED_FIELDS).sort(
        "createdAt", -1).to_list(SCAN_PER_SOURCE)
    uploaded = await JobApplicationLog.get_motor_collection().find(uq, {"jdText": 0}).sort(
        "appliedAt", -1).to_list(SCAN_PER_SOURCE)
    return tailored, uploaded


async def search_matches(account_oid: PydanticObjectId, q: str) -> list[dict]:
    """The profile's resumes whose company contains `q` (normalised), newest first, at most MAX_MATCHES."""
    q_norm = normalize_company(q)
    if len(q_norm.replace(" ", "")) < 2:
        return []
    # Narrow in Mongo by the query's longest word, then compare normalised names here.
    word = max(q_norm.split(), key=len)
    tailored, uploaded = await _candidates(account_oid, {"$regex": re.escape(word), "$options": "i"})
    out = [_tailored_out(d) for d in tailored if q_norm in normalize_company(d.get("companyName"))]
    out += [_uploaded_out(d) for d in uploaded if q_norm in normalize_company(d.get("company"))]
    out.sort(key=lambda m: m["appliedAt"] or datetime.min, reverse=True)
    return [{**m, "appliedAt": _iso(m["appliedAt"])} for m in out[:MAX_MATCHES]]


async def exact_matches(account_oid: PydanticObjectId, company: str, before: Optional[datetime]) -> list[str]:
    """Match ids (newest first) whose company equals `company` once normalised, dated before `before` (the backfill's
    rule: only near-exact names link automatically)."""
    want = normalize_company(company)
    if not want:
        return []
    word = max(want.split(), key=len)
    tailored, uploaded = await _candidates(account_oid, {"$regex": re.escape(word), "$options": "i"})
    found = [(d.get("createdAt"), f"t:{d['_id']}") for d in tailored if normalize_company(d.get("companyName")) == want]
    found += [(d.get("appliedAt"), f"u:{d['_id']}") for d in uploaded if normalize_company(d.get("company")) == want]
    found = [(at, mid) for at, mid in found if at and (before is None or at < before)]
    return [mid for _, mid in sorted(found, key=lambda f: f[0], reverse=True)]


def _bad(detail: str) -> HTTPException:
    return HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=detail)


async def _cached_jd(url: Optional[str]) -> Optional[str]:
    """An applied-log entry from before it kept its JD: a person's correction, else the 30-day posting cache."""
    from app.models.job_apply import JobInfo, JobPostingCache

    if not url:
        return None
    info = await JobInfo.get_motor_collection().find_one({"url": url}, {"jdText": 1})
    if info and info.get("jdText"):
        return info["jdText"]
    cached = await JobPostingCache.get_motor_collection().find_one({"url": url}, {"text": 1})
    return (cached or {}).get("text") or None


async def _tailored_link(oid, account_oid, user, linked_by: str, score_missing: bool) -> InterviewResumeLink:
    from app import resume_retention
    from app.job_apply import tailored_score
    from app.models import ResumeApplication

    coll = ResumeApplication.get_motor_collection()
    d = await coll.find_one(
        {"_id": oid}, {"pdfData": 0, "coverLetterText": 0, "coverLetterHistory": 0, "screeningPairs": 0},
    )
    if not d or d.get("accountId") != account_oid or d.get("status") != "completed":
        raise _bad("That resume isn't one of this profile's finished resumes.")
    jd, text = d.get("jobDescription") or "", d.get("plainText") or ""
    if (not jd or not text) and d.get("textArchiveKey"):
        archived = await resume_retention.archived_text(d["textArchiveKey"], oid) or {}
        jd, text = jd or archived.get("jobDescription") or "", text or archived.get("plainText") or ""
    score = as_match(d.get("matchScore"))
    if score is None and score_missing and text:
        job = SimpleNamespace(
            id=oid, source=d.get("source"), jobApplyRowId=d.get("jobApplyRowId"), jobDescription=jd, plainText=text,
        )
        match = await tailored_score.score_job(job, user)
        if match is not None:
            score = match.model_dump()
            await coll.update_one({"_id": oid}, {"$set": {"matchScore": score}})
    return InterviewResumeLink(
        kind="fixed" if d.get("fixedFrom") else "tailored", linkedBy=linked_by, resumeJobId=oid,
        matchedCompany=d.get("companyName"),
        source=d.get("source") or "generator", filename=_basename(d.get("pdfFilename")),
        promptName=d.get("promptName"), resumeModel=d.get("resumeLlmModel"), appliedAt=d.get("createdAt"),
        jobDescription=jd or None, jobUrl=d.get("jobUrl"), score=score,
    )


async def _uploaded_link(oid, account_oid, linked_by: str) -> InterviewResumeLink:
    from app.models.job_apply import JobApplicationLog

    d = await JobApplicationLog.get_motor_collection().find_one({"_id": oid})
    if not d or d.get("accountId") != str(account_oid) or d.get("tailoredJobId"):
        raise _bad("That application isn't one of this profile's.")
    return InterviewResumeLink(
        kind="uploaded", linkedBy=linked_by, appliedLogId=oid, resumeId=d.get("resumeId"),
        matchedCompany=d.get("company"), source="job_applies", filename=d.get("filename") or None,
        appliedAt=d.get("appliedAt"), jobDescription=d.get("jdText") or await _cached_jd(d.get("url")),
        jobUrl=d.get("url"), score=d.get("score"),
    )


async def build_link(
    match_id: str,
    account_oid: PydanticObjectId,
    user=None,
    *,
    linked_by: Literal["user", "auto"] = "user",
    score_missing: bool = True,
) -> InterviewResumeLink:
    """The snapshot for one search result (`t:<generated resume id>` or `u:<applied-log id>`) of this profile.
    `score_missing`: score a generated resume that never was (one cached LLM extraction for a Generator one)."""
    kind, _, raw = (match_id or "").partition(":")
    if kind not in ("t", "u") or not PydanticObjectId.is_valid(raw):
        raise _bad("Unknown resume match.")
    oid = PydanticObjectId(raw)
    if kind == "t":
        return await _tailored_link(oid, account_oid, user, linked_by, score_missing)
    return await _uploaded_link(oid, account_oid, linked_by)


class ResumeLinkIn(BaseModel):
    """What the interview form sends: a picked search result, or that none was found and why."""

    matchId: Optional[str] = None
    none: bool = False
    reason: Optional[NoMatchReason] = None
    note: Optional[str] = None


async def resolve_link(body: ResumeLinkIn, account_oid: PydanticObjectId, user) -> InterviewResumeLink:
    if body.none:
        note = (body.note or "").strip()
        if body.reason is None:
            raise _bad("Pick why no company was found.")
        if body.reason == "fixed_resume":  # forks replaced it: the company search finds the fixed resume
            raise _bad("Search the company: a fixed resume used in Auto apply is found there.")
        if body.reason == "other" and not note:
            raise _bad("Add a note for the reason Other.")
        return InterviewResumeLink(kind="none", reason=body.reason, reasonNote=note or None)
    if not body.matchId:
        raise _bad("Pick a resume, or tick No company found.")
    return await build_link(body.matchId, account_oid, user)


async def linked_interview_names(job_id: PydanticObjectId, limit: int = 5) -> list[str]:
    """Companies of the interviews linked to this generated resume (at most `limit`): deleting it would leave their
    resume download with nothing to fetch."""
    cursor = Interview.get_motor_collection().find({"resumeLink.resumeJobId": job_id}, {"companyName": 1}).limit(limit)
    return [(d.get("companyName") or "").strip() or "an untitled company" async for d in cursor]
