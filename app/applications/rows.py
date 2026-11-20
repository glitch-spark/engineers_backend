"""Pure building of `applications` rows from bids and tailored resumes, and matching interviews to them.

Inputs (what the sync must pass): plain dicts (raw Mongo docs are fine) with naive-UTC datetimes; interviews as
documents; `resume_row` keyed by str(resume _id) -> row key; `accounts` as {accountId: {"region", "createdAt"}}; resumes may carry `jobDescription`, bids `jobKey` / `jobTitle`.
A bid's `accountId` must be its resolved profile: the sync fills it from Bidder.profileId for bidder bids."""

import re
from datetime import datetime, timezone
from itertools import chain
from typing import Optional
from urllib.parse import urlparse

from app.applications.area import classify_area, job_title
from app.caller_stats import round_result
from app.interview_rounds import _breakdown_group, merged_rounds
from app.models.resume_application import as_match, job_url_key

_TWO_PART_SUFFIXES = frozenset({
    "co.uk", "org.uk", "ac.uk", "com.au", "net.au", "co.nz", "co.jp", "co.in", "co.za", "com.br", "com.mx", "com.sg",
})
_MAX = datetime.max
_STATUS_RANK = {"approved": 2, "pending": 1, "rejected": 0}
_ORDER = ("intro", "tech", "hiring", "panel", "final")


def board_of(url: Optional[str]) -> Optional[str]:
    """The job link's registrable domain ("jobs.ashbyhq.com" -> "ashbyhq.com")."""
    raw = (url or "").strip()
    if not raw:
        return None
    host = (urlparse(raw if "://" in raw else "//" + raw).hostname or "").lower()
    host = re.sub(r"^www\.", "", host)
    labels = [p for p in host.split(".") if p]
    if len(labels) < 2:
        return None
    n = 3 if ".".join(labels[-2:]) in _TWO_PART_SUFFIXES else 2
    return ".".join(labels[-n:])


def _key(account_id, url, coll: str, doc_id) -> tuple[str, Optional[str]]:
    jk = job_url_key(url) if account_id is not None else None  # no profile: never merge across owners
    return (f"{account_id}:{jk}" if jk else f"src:{coll}:{doc_id}"), jk


def build_rows(bids: list[dict], resumes: list[dict], accounts: dict,
               tailored: Optional[list[dict]] = None) -> dict[str, dict]:
    """One row per (accountId, job link); a bid and a tailored resume for the same job merge, the bidder wins `by`.
    `tailored` = unapplied resumes that are not row sources: the earliest one per job fills a bid row's missing
    score/prompt/template/title and sets `tailoredResumeId`; it never adds a row or a source."""
    rows: dict[str, dict] = {}
    bid_titles: dict[str, list] = {}  # row key -> its bids' (jobTitle, jobKey), earliest first: the title fallbacks

    def row_for(key, jk, account_id, owner_id, url, at):
        r = rows.get(key)
        if r is None:
            r = rows[key] = {
                "key": key, "accountId": account_id, "ownerId": owner_id, "region": (accounts.get(account_id) or {}).get("region"),
                "jobKey": jk, "board": board_of(url), "appliedAt": at, "by": "self", "sources": [],
            }
        elif at < r["appliedAt"]:
            r["appliedAt"], r["ownerId"] = at, owner_id  # a shared row belongs to its earliest source's owner
        return r

    for b in sorted(bids, key=lambda b: b["firstAt"]):
        key, jk = _key(b.get("accountId"), b.get("jobUrl"), "bids", b["_id"])
        r = row_for(key, jk, b.get("accountId"), b["ownerId"], b.get("jobUrl"), b["firstAt"])
        if b.get("kind", "bidder") == "bidder":
            r["by"] = "bidder"
            r.setdefault("bidderId", b.get("bidderId"))  # the earliest bidder bid's
        st = b.get("status")
        if key not in bid_titles or _STATUS_RANK.get(st, -1) > _STATUS_RANK.get(r["bidStatus"], -1):
            r["bidStatus"] = st
        r["sources"].append(f"bid:{b['_id']}")
        bid_titles.setdefault(key, []).append((b.get("jobTitle"), b.get("jobKey")))

    for ra in sorted((x for x in resumes if x.get("countsAsBid") is not False), key=lambda x: x["createdAt"]):
        key, jk = _key(ra.get("accountId"), ra.get("jobUrl"), "resumeapplications", ra["_id"])
        r = row_for(key, jk, ra.get("accountId"), ra["userId"], ra.get("jobUrl"), ra["createdAt"])
        first = not any(s.startswith("resume:") for s in r["sources"])
        r["sources"].append(f"resume:{ra['_id']}")
        if not first:
            continue  # score/prompt/template/title come from the earliest resume
        m = as_match(ra.get("matchScore")) or {}
        r["score"], r["scoreBand"] = m.get("total"), m.get("band")
        for f in ("promptId", "promptName", "templateName"):
            r[f] = ra.get(f)
        r["title"] = job_title(ra.get("jobDescription"), None)

    earliest: dict[str, dict] = {}  # bid row key -> its earliest tailored resume
    for ra in sorted(tailored or [], key=lambda x: x["createdAt"]):
        key = _key(ra.get("accountId"), ra.get("jobUrl"), "resumeapplications", ra["_id"])[0]
        if key in bid_titles:
            earliest.setdefault(key, ra)
    for key, ra in earliest.items():
        r = rows[key]
        m = as_match(ra.get("matchScore")) or {}
        for f, v in (("score", m.get("total")), ("scoreBand", m.get("band")), ("promptId", ra.get("promptId")),
                     ("promptName", ra.get("promptName")), ("templateName", ra.get("templateName")),
                     ("title", job_title(ra.get("jobDescription"), None))):
            if r.get(f) is None:
                r[f] = v
        r["tailoredResumeId"] = str(ra["_id"])

    for key, r in rows.items():
        ts = bid_titles.get(key, [])  # resume title, else a bid's saved jobTitle, else a readable jobKey
        r["title"] = r.get("title") or next(filter(None, chain(
            (job_title(t, None) for t, _ in ts), (job_title(None, k) for _, k in ts))), None)
        r["area"] = classify_area(r["title"])
        created = (accounts.get(r["accountId"]) or {}).get("createdAt")
        r["profileAgeDays"] = max(0, (r["appliedAt"] - created).days) if created else None
    return rows


def _naive_utc(dt):
    return dt.astimezone(timezone.utc).replace(tzinfo=None) if dt.tzinfo else dt


def interview_outcome(iv) -> Optional[dict]:
    """{firstRoundAt, furthest, result} from the countable, non-canceled rounds; None when there are none."""
    rounds = [(r, _breakdown_group(r.stage)) for r in merged_rounds(iv) if r.status != "canceled"]
    rounds = [(r, g) for r, g in rounds if g]
    if not rounds:
        return None
    dates = [_naive_utc(r.at) for r, _ in rounds if r.at]
    top = max(rounds, key=lambda rg: _ORDER.index(rg[1]))  # max keeps the first of equals
    last_top = [rg for rg in rounds if rg[1] == top[1]][-1][0]
    return {"firstRoundAt": min(dates) if dates else None, "furthest": top[1], "result": round_result(last_top.status)}


def match_interviews(rows: dict[str, dict], interviews: list, resume_row: dict[str, str]) -> tuple[int, int]:
    """Set the interview fields on each row's earliest matching interview. Returns (matched, unmatched)."""
    best: dict[str, tuple] = {}
    unmatched = 0
    for iv in interviews:
        out = interview_outcome(iv)
        if not out:
            continue
        link = getattr(iv, "resumeLink", None)
        key = resume_row.get(str(getattr(link, "resumeJobId", None) or ""))
        if key not in rows:
            acc = getattr(iv, "accountId", None)
            jk = job_url_key(getattr(iv, "jobUrl", None)) if acc is not None else None
            key = f"{acc}:{jk}" if jk else None
        if key not in rows:
            unmatched += 1
            continue
        cur = best.get(key)
        if cur is None or (out["firstRoundAt"] or _MAX) < (cur[1]["firstRoundAt"] or _MAX):
            best[key] = (iv, out)
    for key, (iv, out) in best.items():
        r = rows[key]
        at = out["firstRoundAt"]
        r.update(interviewId=iv.id, firstRoundAt=at, furthest=out["furthest"], result=out["result"],
                 daysToInterview=max(0, (at - r["appliedAt"]).days) if at else None)
        fit = getattr(iv, "fitScore", None)
        if fit and r.get("score") is None:  # no tailored score: the interview's fit score (scripts/score_interview_fit.py)
            r["score"], r["scoreBand"] = fit.get("total"), fit.get("band")
    return len(best), unmatched

