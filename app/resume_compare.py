"""Statistics → Resume compare: the jobs that got an interview in a week or month, one row each, with every user's
resume for the same job (tailored or a fixed-resume fork) and its score, so everyone can see whose resume got the
interview and how it scored against the others'.

A job is its link (ResumeApplication.jobUrlKey, normalised as Job Applies matches links); an interview's link is its
own job URL, else its linked resume's. A user's cell holds, per profile, their latest finished resume for that link.
A region keeps the interviews and resumes of that region's profiles (as Interview rate does); All keeps everything.
Resume links are signed file links (app/job_apply/file_links.py), so every user can open them. Resumes made before
jobUrlKey existed (until scripts/backfill_job_url_keys.py runs) are matched by their job URL's host, then normalised.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Optional

from app.interview_rates import Period, Region, _profile_regions, label, window
from app.models.resume_application import as_match, job_url_key

MAX_ROWS = 300


def _naive(dt: Optional[datetime]) -> Optional[datetime]:
    return dt.replace(tzinfo=None) if dt is not None and dt.tzinfo else dt


def _latest_round(iv, first: datetime, last: datetime) -> tuple[Optional[str], Optional[datetime]]:
    """The interview's latest round held in the window: (stage, when, naive UTC)."""
    rounds = [e for e in iv.stageHistory if e.scheduledAt and first <= _naive(e.scheduledAt) <= last]
    if not rounds:
        return getattr(iv.stage, "value", iv.stage), _naive(iv.scheduledAt)
    r = max(rounds, key=lambda e: _naive(e.scheduledAt))
    return r.stage, _naive(r.scheduledAt)


async def _resumes_for(keys: list[str], user_ids: list) -> list[dict]:
    """The users' finished resumes for these job links, each with its `jobUrlKey`: by the stored key, and (for resumes
    made before it was stored) by their URL's host, normalised here."""
    from app.models import ResumeApplication

    hosts = sorted({k.split("/", 1)[0].split(":", 1)[0] for k in keys} - {""})
    unkeyed = {"jobUrlKey": None, "jobUrl": {"$regex": "|".join(re.escape(h) for h in hosts), "$options": "i"}}
    cursor = ResumeApplication.get_motor_collection().find(
        {"status": "completed", "userId": {"$in": user_ids},
         "$or": [{"jobUrlKey": {"$in": keys}}, *([unkeyed] if hosts else [])]},
        {"jobUrl": 1, "jobUrlKey": 1, "userId": 1, "accountId": 1, "profileName": 1, "fixedFrom": 1, "matchScore": 1,
         "createdAt": 1, "companyName": 1, "promptName": 1},
    )
    wanted, out = set(keys), []
    async for d in cursor:
        d["jobUrlKey"] = d.get("jobUrlKey") or job_url_key(d.get("jobUrl"))
        if d["jobUrlKey"] in wanted:
            out.append(d)
    return out


async def resume_compare(viewer, period: Period, day: date, q: str = "", region: Region = "all") -> dict:
    from app.dashboard_stats import app_tz, local_day_start_utc
    from app.interview_resume_link import normalize_company
    from app.interview_rounds import load_interviews_for_window
    from app.job_apply import file_links
    from app.models import Account, ResumeApplication, UserExtra  # noqa: F401 — ResumeApplication: linked resumes
    from app.routers.metrics import _display_name

    users = await UserExtra.find({"role": {"$ne": "admin"}}).to_list()
    by_id = {u.id: u for u in users}
    start, end = window(period, day)
    tz = app_tz()
    first, last = local_day_start_utc(start, tz), local_day_start_utc(end + timedelta(days=1), tz) - timedelta(microseconds=1)
    regions, _ = await _profile_regions()

    def in_region(account_id) -> bool:
        return region == "all" or regions.get(account_id) == region

    interviews = [iv for iv in await load_interviews_for_window(list(by_id), first, last) if in_region(iv.accountId)]

    # The interviews' own resumes (for a link when the interview has none, and to mark the one it came from).
    linked_ids = [iv.resumeLink.resumeJobId for iv in interviews if iv.resumeLink and iv.resumeLink.resumeJobId]
    linked = {
        d["_id"]: d async for d in ResumeApplication.get_motor_collection().find(
            {"_id": {"$in": linked_ids}}, {"jobUrl": 1, "jobUrlKey": 1})
    } if linked_ids else {}

    rows: dict[str, dict] = {}
    want = normalize_company(q)
    for iv in interviews:
        link = iv.resumeLink
        key = job_url_key(iv.jobUrl) or job_url_key(link.jobUrl if link else None)
        if not key and link and link.resumeJobId in linked:
            key = linked[link.resumeJobId].get("jobUrlKey") or job_url_key(linked[link.resumeJobId].get("jobUrl"))
        if not key or (want and want not in normalize_company(iv.companyName)):
            continue
        stage, when = _latest_round(iv, first, last)
        row = rows.setdefault(key, {"key": key, "company": iv.companyName or "", "jobUrl": iv.jobUrl or key,
                                    "interviews": [], "_last": when})
        row["interviews"].append({
            "userId": str(iv.createdBy), "name": _display_name(by_id[iv.createdBy]) if iv.createdBy in by_id else "",
            "accountId": str(iv.accountId), "stage": stage,
            "date": when.isoformat() + "Z" if when else None,
            "resumeJobId": str(link.resumeJobId) if link and link.resumeJobId else None,
        })
        if when and (row["_last"] is None or when > row["_last"]):
            row["_last"] = when

    ordered = sorted(rows.values(), key=lambda r: r["_last"] or datetime.min, reverse=True)[:MAX_ROWS]
    keys = [r["key"] for r in ordered]
    resumes = await _resumes_for(keys, list(by_id)) if keys else []
    profile_names = {
        a["_id"]: a.get("name") or "" async for a in Account.get_motor_collection().find(
            {"_id": {"$in": list({d.get("accountId") for d in resumes})}}, {"name": 1})
    } if resumes else {}

    # Per row: per (user, profile) the resume the interview links to, else the latest one.
    for row in ordered:
        mine = [d for d in resumes if d["jobUrlKey"] == row["key"] and in_region(d.get("accountId"))]
        chosen = {i["resumeJobId"] for i in row["interviews"] if i["resumeJobId"]}
        picked: dict = {}
        for d in sorted(mine, key=lambda d: d.get("createdAt") or datetime.min):
            slot = (d["userId"], d.get("accountId"))
            if slot not in picked or str(picked[slot]["_id"]) not in chosen:
                picked[slot] = d
        interviewed = {(i["userId"], i["accountId"]) for i in row["interviews"]}
        cells: dict[str, list] = {}
        for (uid, acc), d in picked.items():
            score = as_match(d.get("matchScore"))
            cells.setdefault(str(uid), []).append({
                "resumeJobId": str(d["_id"]),
                "profileName": profile_names.get(acc) or d.get("profileName") or "",
                "kind": "fixed" if d.get("fixedFrom") else "tailored",
                "promptName": d.get("promptName"),
                "score": {"total": score["total"], "band": score["band"], "missing": score.get("missing") or []}
                if score else None,
                "link": file_links.file_url("tailored", str(d["_id"])),
                "interviewed": (str(uid), str(acc)) in interviewed,
            })
        # An interviewed profile with no resume for the link still shows that it got the interview.
        for i in row["interviews"]:
            entries = cells.setdefault(i["userId"], [])
            if not any(e["interviewed"] for e in entries):
                entries.append({"resumeJobId": None, "profileName": profile_names.get(i["accountId"], ""),
                                "kind": None, "promptName": None, "score": None, "link": None, "interviewed": True})
        row["cells"] = cells
        del row["_last"]

    shown = {uid for r in ordered for uid in r["cells"]} | {str(viewer.id)}
    columns = sorted(
        (u for u in users if str(u.id) in shown),
        key=lambda u: (u.id != viewer.id, _display_name(u).lower()),
    )
    return {
        "period": period, "region": region, "start": start.isoformat(), "end": end.isoformat(), "label": label(period, start, end),
        "users": [{"userId": str(u.id), "name": _display_name(u), "isMe": u.id == viewer.id} for u in columns],
        "rows": ordered,
    }
