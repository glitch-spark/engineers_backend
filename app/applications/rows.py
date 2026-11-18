"""Pure building of `applications` rows from bids and tailored resumes, and matching interviews to them."""

import re
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlparse

from app.caller_stats import round_result
from app.interview_rounds import _breakdown_group, merged_rounds
from app.models.resume_application import job_url_key

_TWO_PART_SUFFIXES = frozenset({
    "co.uk", "org.uk", "ac.uk", "com.au", "net.au", "co.nz", "co.jp", "co.in", "co.za", "com.br", "com.mx", "com.sg",
})
_MAX = datetime.max
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
    jk = job_url_key(url)
    return (f"{account_id}:{jk}" if jk else f"src:{coll}:{doc_id}"), jk


def build_rows(bids: list[dict], resumes: list[dict], regions: dict) -> dict[str, dict]:
    """One row per (accountId, job link); a bid and a tailored resume for the same job merge, the bidder wins `by`."""
    rows: dict[str, dict] = {}

    def row_for(key, jk, account_id, owner_id, url, at):
        r = rows.get(key)
        if r is None:
            r = rows[key] = {
                "key": key, "accountId": account_id, "ownerId": owner_id, "region": regions.get(account_id),
                "jobKey": jk, "board": board_of(url), "appliedAt": at, "by": "self", "sources": [],
            }
        elif at < r["appliedAt"]:
            r["appliedAt"] = at
        return r

    for b in bids:
        key, jk = _key(b.get("accountId"), b.get("jobUrl"), "bids", b["_id"])
        r = row_for(key, jk, b.get("accountId"), b["ownerId"], b.get("jobUrl"), b["firstAt"])
        if b.get("kind", "bidder") == "bidder":
            r["by"], r["bidderId"] = "bidder", b.get("bidderId")
        r["bidStatus"] = b.get("status")
        r["sources"].append("bid")

    for ra in resumes:
        if ra.get("countsAsBid") is False:
            continue
        key, jk = _key(ra.get("accountId"), ra.get("jobUrl"), "resumeapplications", ra["_id"])
        r = row_for(key, jk, ra.get("accountId"), ra["userId"], ra.get("jobUrl"), ra["createdAt"])
        m = ra.get("matchScore") or {}
        r["score"], r["scoreBand"] = m.get("total"), m.get("band")
        for f in ("promptId", "promptName", "templateName"):
            r[f] = ra.get(f)
        r["sources"].append("resume")
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
            jk = job_url_key(getattr(iv, "jobUrl", None))
            key = f"{getattr(iv, 'accountId', None)}:{jk}" if jk else None
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
    return len(best), unmatched

