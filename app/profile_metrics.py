"""Profile metrics: a profile's weekly bids, interviews and scores over its last completed weeks.

Weeks run Monday to Sunday and only completed ones count. A profile is measured from the latest of the window's
first Monday, the first Monday on or after PROFILE_METRICS_SINCE and the Monday of the week it was created.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from beanie import PydanticObjectId

from app.bids.windows import BidSchedule, schedule_from_settings
from app.config import get_settings
from app.dashboard_stats import app_tz, local_day_start_utc
from app.interview_rates import MIN_BIDS
from app.interview_rounds import split_rounds_by_day
from app.models import Account, Bid, Bidder, DailyPlan, Interview, JobApplicationLog
from app.models.resume_application import ResumeApplication, as_match

WEEK_CHOICES = (4, 8, 12)


def since_date() -> date:
    """PROFILE_METRICS_SINCE as a date; date.min when unset."""
    s = get_settings().PROFILE_METRICS_SINCE
    return date.fromisoformat(s) if s else date.min


def span(weeks: int, today: date, since: date, created: date) -> tuple[date, date, int]:
    """(first Monday, last completed Sunday, weeks between them); 0 weeks when the start is after the end."""
    this_monday = today - timedelta(days=today.weekday())
    end = this_monday - timedelta(days=1)
    start = max(
        this_monday - timedelta(weeks=weeks),
        since + timedelta(days=-since.weekday() % 7),
        created - timedelta(days=created.weekday()),
    )
    return start, end, max(0, ((end - start).days + 1) // 7)


def bid_report_day(first_at: datetime, sched: BidSchedule) -> date:
    """The local date of the Bid Track daily window a naive-UTC time falls in (a bid at the cutoff is the next day's)."""
    local = first_at.replace(tzinfo=timezone.utc).astimezone(sched.tz)
    return local.date() + timedelta(days=1 if local.time() >= sched.daily_at else 0)


_COUNTS = ("selfBids", "bidderBids", "selfInterviews", "callerInterviews")


def _monday(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _midnight(day: date) -> datetime:
    return datetime(day.year, day.month, day.day)


async def self_bids(owner_ids: list, account_ids: set[str], start: date, end: date) -> dict[tuple[str, date], int]:
    """(account id, Monday) -> self bids typed per profile in the owners' daily plans; logged (followed-up) days only."""
    plans = await DailyPlan.find({
        "userId": {"$in": owner_ids}, "date": {"$gte": _midnight(start), "$lte": _midnight(end)},
        "loggedAt": {"$ne": None},  # a goal-only day hasn't been followed up: no bids yet
    }).to_list()
    out: dict = defaultdict(int)
    for p in plans:
        for account_id, n in p.done.bidsSelfByProfile.items():
            if account_id in account_ids:
                out[(account_id, _monday(p.date.date()))] += n
    return dict(out)


async def bidder_bids(
    account_ids: list[PydanticObjectId], start: date, end: date, sched: BidSchedule,
) -> dict[tuple[str, date], int]:
    """(account id, Monday) -> approved and pending bids of the profile's bidders (archived ones too), by report day."""
    profile_of = {b.id: str(b.profileId) async for b in Bidder.find({"profileId": {"$in": account_ids}})}
    # Report days don't align with UTC midnight: query a day wider on each side, then filter by report day.
    bids = await Bid.find({
        "bidderId": {"$in": list(profile_of)}, "status": {"$in": ["approved", "pending"]},
        "firstAt": {"$gte": _midnight(start) - timedelta(days=1), "$lt": _midnight(end) + timedelta(days=2)},
    }).to_list()
    out: dict = defaultdict(int)
    for b in bids:
        day = bid_report_day(b.firstAt, sched)
        if start <= day <= end:
            out[(profile_of[b.bidderId], _monday(day))] += 1
    return dict(out)


async def interview_rounds(
    account_ids: list[PydanticObjectId], start: date, end: date,
) -> dict[tuple[str, date], list[int]]:
    """(account id, Monday) -> [self rounds, caller rounds] held on the app's local days, by the interview's profile."""
    tz = app_tz()
    first = local_day_start_utc(start, tz)
    last = local_day_start_utc(end + timedelta(days=1), tz) - timedelta(microseconds=1)
    ivs = await Interview.find({
        "accountId": {"$in": account_ids},
        "$or": [
            {"scheduledAt": {"$gte": first, "$lte": last}},
            {"stageHistory.scheduledAt": {"$gte": first, "$lte": last}},
        ],
    }).to_list()
    out: dict = defaultdict(lambda: [0, 0])
    for iv in ivs:
        for day, _group, is_caller, _stage in split_rounds_by_day(iv, first, last, tz):
            out[(str(iv.accountId), _monday(day))][int(is_caller)] += 1
    return dict(out)


async def _dated_scores(account_ids: list[str], start: date, end: date) -> list[tuple[str, date, str, int]]:
    """(accountId, local day applied, "tailored" | "match", score) for the jobs applied to in [start, end].

    A tailored resume scores as its application's match; a fixed-resume fork is an uploaded resume, so "match".
    An uploaded resume (resumeId) uses the score kept on its log entry. No score: skipped."""
    tz = app_tz()
    entries = await JobApplicationLog.find({
        "accountId": {"$in": account_ids},
        "appliedAt": {"$gte": local_day_start_utc(start, tz), "$lt": local_day_start_utc(end + timedelta(days=1), tz)},
    }).to_list()
    ids = [PydanticObjectId(e.tailoredJobId) for e in entries if e.tailoredJobId and PydanticObjectId.is_valid(e.tailoredJobId)]
    apps = {}
    if ids:
        cursor = ResumeApplication.get_motor_collection().find({"_id": {"$in": ids}}, {"matchScore": 1, "fixedFrom": 1})
        apps = {d["_id"]: d async for d in cursor}
    out = []
    for e in entries:
        if e.tailoredJobId:
            doc = apps.get(PydanticObjectId(e.tailoredJobId)) if PydanticObjectId.is_valid(e.tailoredJobId) else None
            doc = doc or {}
            match = as_match(doc.get("matchScore"))  # a bare number on old records
            total = match.get("total") if isinstance(match, dict) else None
            kind = "match" if doc.get("fixedFrom") else "tailored"
        elif e.resumeId and e.score:
            total, kind = e.score.total, "match"
        else:
            continue
        if total is not None:
            day = e.appliedAt.replace(tzinfo=timezone.utc).astimezone(tz).date()
            out.append((e.accountId, day, kind, int(total)))
    return out


async def scores(account_ids: list[str], start: date, end: date) -> dict[str, dict[str, list[int]]]:
    """accountId -> {"tailored": [...], "match": [...]} resume scores of the jobs applied to in [start, end]."""
    out = {a: {"tailored": [], "match": []} for a in account_ids}
    for account_id, _day, kind, total in await _dated_scores(account_ids, start, end):
        out[account_id][kind].append(total)
    return out


def _per_week(total: int, n_weeks: int) -> Optional[float]:
    return round(total / n_weeks, 1) if n_weeks else None


def _score(values: list[int]) -> dict:
    return {"avg": round(sum(values) / len(values), 1) if values else None, "n": len(values)}


def summarize(account_id: str, start: date, n_weeks: int, weekly: dict, score_lists: dict) -> dict:
    """One profile's row. `weekly`: Monday -> {selfBids, bidderBids, selfInterviews, callerInterviews}, already
    clipped to its span. No weeks measured: nothing per week and no rate."""
    weeks = [weekly.get(start + timedelta(weeks=i), {}) for i in range(n_weeks)]
    total = {k: sum(w.get(k, 0) for w in weeks) for k in _COUNTS}
    bids = total["selfBids"] + total["bidderBids"]
    interviews = total["selfInterviews"] + total["callerInterviews"]
    return {
        "accountId": account_id,
        "spanWeeks": n_weeks,
        "bidsPerWeek": _per_week(bids, n_weeks),
        "selfBidsPerWeek": _per_week(total["selfBids"], n_weeks),
        "bidderBidsPerWeek": _per_week(total["bidderBids"], n_weeks),
        "interviewsPerWeek": _per_week(interviews, n_weeks),
        "selfInterviewsPerWeek": _per_week(total["selfInterviews"], n_weeks),
        "callerInterviewsPerWeek": _per_week(total["callerInterviews"], n_weeks),
        "interviewsPerBid": round(interviews / bids, 4) if bids and n_weeks else None,
        "lowVolume": bids < MIN_BIDS,
        "tailorScore": _score(score_lists["tailored"]),
        "matchScore": _score(score_lists["match"]),
    }


def peer_average(rows: list[dict]) -> Optional[dict]:
    """The mean of each value over the rows where it isn't null (scores: mean of averages, summed n); None without rows."""
    if not rows:
        return None

    def mean(field: str, digits: int) -> Optional[float]:
        vals = [r[field] for r in rows if r[field] is not None]
        return round(sum(vals) / len(vals), digits) if vals else None

    def score(field: str) -> dict:
        avgs = [r[field]["avg"] for r in rows if r[field]["avg"] is not None]
        return {"avg": round(sum(avgs) / len(avgs), 1) if avgs else None, "n": sum(r[field]["n"] for r in rows)}

    out: dict = {f: mean(f, 1) for f in (
        "bidsPerWeek", "selfBidsPerWeek", "bidderBidsPerWeek", "interviewsPerWeek", "selfInterviewsPerWeek",
        "callerInterviewsPerWeek",
    )}
    out["interviewsPerBid"] = mean("interviewsPerBid", 4)
    out["tailorScore"], out["matchScore"] = score("tailorScore"), score("matchScore")
    return out


async def collect(accounts: list[Account], weeks: int, today: date) -> dict[str, tuple[date, int, dict, dict]]:
    """accountId -> (start Monday, weeks measured, weekly counts, score lists) over each profile's own span; every
    source (scores too) is loaded once over the widest of them and clipped to each profile's span."""
    tz, since = app_tz(), since_date()
    spans = {}
    for a in accounts:
        created = a.createdAt.replace(tzinfo=timezone.utc).astimezone(tz).date()
        spans[str(a.id)] = span(weeks, today, since, created)
    out = {k: (s, n, {}, {"tailored": [], "match": []}) for k, (s, _e, n) in spans.items()}
    live = [a for a in accounts if spans[str(a.id)][2]]
    if not live:
        return out
    start, end = min(spans[str(a.id)][0] for a in live), max(spans[str(a.id)][1] for a in live)
    keys, ids = {str(a.id) for a in live}, [a.id for a in live]
    loaded = {
        "selfBids": await self_bids(list({a.createdBy for a in live}), keys, start, end),
        "bidderBids": await bidder_bids(ids, start, end, schedule_from_settings()),
    }
    rounds = await interview_rounds(ids, start, end)
    dated = await _dated_scores(list(keys), start, end)
    for key in keys:
        first, _e, n = spans[key]
        weekly = {first + timedelta(weeks=i): dict.fromkeys(_COUNTS, 0) for i in range(n)}
        for name, counts in loaded.items():
            for (k, monday), v in counts.items():
                if k == key and monday in weekly:
                    weekly[monday][name] = v
        for (k, monday), (mine, caller) in rounds.items():
            if k == key and monday in weekly:
                weekly[monday]["selfInterviews"], weekly[monday]["callerInterviews"] = mine, caller
        score_lists = {"tailored": [], "match": []}
        for k, day, kind, total in dated:
            if k == key and first <= day <= spans[key][1]:
                score_lists[kind].append(total)
        out[key] = (first, n, weekly, score_lists)
    return out
