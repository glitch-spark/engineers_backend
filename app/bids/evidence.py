"""Evidence shown next to a bid in the review: where it was filed, how long it took, which profile and resumes the
bidder used. Profiles and tailored resumes are read with one query per collection, not one per bid."""

from datetime import datetime
from typing import Optional
from urllib.parse import urlsplit

from app.models.account import Account
from app.models.bid import Bid
from app.models.bidder import Bidder
from app.models.resume_application import ResumeApplication


def job_domain(url: Optional[str]) -> Optional[str]:
    """Host of the job URL without a leading www.; None without a (parsable) URL."""
    try:
        host = urlsplit(url).hostname if url else None
    except ValueError:
        return None
    return host.removeprefix("www.") if host else None


def duration_sec(bid: Bid) -> int:
    """Seconds from the first to the last screenshot; 0 for a single screenshot."""
    times = [s.capturedAt for s in bid.screenshots]
    return int((max(times) - min(times)).total_seconds()) if len(times) > 1 else 0


def _newest(doc: dict) -> datetime:
    return doc.get("completedAt") or doc.get("createdAt") or datetime.min


async def evidence_for(bids: list[Bid], bidders: dict[str, Bidder]) -> dict[str, dict]:
    """bid id -> {profileName, resumeNames, tailoredResumeName}. `bidders` is keyed by bidder id (string).

    The tailored resume is the newest completed ResumeApplication of the bidder's profile for exactly the bid's jobUrl.
    Only names are read: an Account holds its resumes' markdown and a ResumeApplication its PDF."""
    profile_of = {str(b.id): (bidders[str(b.bidderId)].profileId if str(b.bidderId) in bidders else None) for b in bids}
    profile_ids = list({p for p in profile_of.values() if p})

    profiles: dict = {}
    if profile_ids:
        cursor = Account.get_motor_collection().find({"_id": {"$in": profile_ids}}, {"name": 1, "resumes.filename": 1})
        profiles = {d["_id"]: d async for d in cursor}

    urls = list({b.jobUrl for b in bids if b.jobUrl and profile_of[str(b.id)] in profiles})
    tailored: dict[tuple, dict] = {}
    if urls:
        cursor = ResumeApplication.get_motor_collection().find(
            {"accountId": {"$in": profile_ids}, "jobUrl": {"$in": urls}, "status": "completed"},
            {"accountId": 1, "jobUrl": 1, "pdfFilename": 1, "completedAt": 1, "createdAt": 1},
        )
        async for doc in cursor:
            key = (doc["accountId"], doc["jobUrl"])
            if key not in tailored or _newest(doc) > _newest(tailored[key]):
                tailored[key] = doc

    out: dict[str, dict] = {}
    for b in bids:
        profile_id = profile_of[str(b.id)]
        profile = profiles.get(profile_id)
        match = tailored.get((profile_id, b.jobUrl)) if profile and b.jobUrl else None
        out[str(b.id)] = {
            "profileName": profile["name"] if profile else None,
            "resumeNames": [r["filename"] for r in profile.get("resumes", [])] if profile else [],
            "tailoredResumeName": match.get("pdfFilename") if match else None,
        }
    return out
