"""Profile metrics: a profile's weekly bids, interviews and scores over its last completed weeks.

Weeks run Monday to Sunday and only completed ones count. A profile is measured from the latest of the window's
first Monday, the first Monday on or after the launch day (`since_date`) and the Monday of the week it was created.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Optional

from beanie import PydanticObjectId
from pydantic import BaseModel, Field

from app.bids.windows import BidSchedule, schedule_from_settings
from app.config import get_settings
from app.dashboard_stats import app_tz, local_day_start_utc
from app.interview_rates import MIN_BIDS
from app.interview_rounds import split_rounds_by_day
from app.models import Account, Bid, Bidder, DailyPlan, Interview, JobApplicationLog
from app.models.resume_application import ResumeApplication, as_match

WEEK_CHOICES = (4, 8, 12)


class ProfileSpan(BaseModel):
    """The Account fields `collect` reads: metrics project these, since a whole profile carries its templates and
    resumes."""

    id: PydanticObjectId = Field(alias="_id")
    createdBy: PydanticObjectId
    createdAt: datetime = Field(default_factory=datetime.utcnow)  # as on Account (TimestampedDocument)


# Where the deployed backend records the day self bids start being typed per profile (one document, never updated).
_LAUNCH = {"_id": "profileMetricsSince"}
_launched: Optional[date] = None


def _settings_collection():
    return Account.get_motor_collection().database["app_settings"]


async def record_launch(today: date) -> None:
    """Called at startup by the deployed backend only: the first release to run there starts metrics the next day.
    Later restarts and deploys keep the date already recorded."""
    await _settings_collection().update_one(
        _LAUNCH, {"$setOnInsert": {"value": (today + timedelta(days=1)).isoformat(), "at": datetime.utcnow()}},
        upsert=True,
    )


async def since_date() -> Optional[date]:
    """PROFILE_METRICS_SINCE when set, else the recorded launch day; None before the deployed backend recorded one
    (nothing is measured yet)."""
    global _launched
    if s := get_settings().PROFILE_METRICS_SINCE:
        return date.fromisoformat(s)
    if _launched is None:
        doc = await _settings_collection().find_one(_LAUNCH)
        _launched = date.fromisoformat(doc["value"]) if doc else None
    return _launched


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


_COUNTS = ("selfBids", "bidderBids", "selfInterviews", "callerInterviews",
           # the same interviews by who applied for the job (Interview.appliedBy; unset = not recorded)
           "selfAppliedInterviews", "bidderAppliedInterviews", "unsetAppliedInterviews")
_APPLIED = {"self": 2, "bidder": 3}  # interview_rounds' slot per appliedBy kind; anything else is unset (4)


def _monday(day: date) -> date:
    return day - timedelta(days=day.weekday())


def _midnight(day: date) -> datetime:
    return datetime(day.year, day.month, day.day)


async def self_bids(owner_ids: list, account_ids: set[str], start: date, end: date) -> dict[tuple[str, date], int]:
    """(account id, day) -> self bids typed per profile in the owners' daily plans; logged (followed-up) days only."""
    plans = await DailyPlan.find({
        "userId": {"$in": owner_ids}, "date": {"$gte": _midnight(start), "$lte": _midnight(end)},
        "loggedAt": {"$ne": None},  # a goal-only day hasn't been followed up: no bids yet
    }).to_list()
    out: dict = defaultdict(int)
    for p in plans:
        for account_id, n in p.done.bidsSelfByProfile.items():
            if account_id in account_ids:
                out[(account_id, p.date.date())] += n
    return dict(out)


async def bidder_bids(
    account_ids: list[PydanticObjectId], start: date, end: date, sched: BidSchedule,
) -> dict[tuple[str, date], int]:
    """(account id, report day) -> approved and pending bids of the profile's bidders (archived ones too)."""
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
            out[(profile_of[b.bidderId], day)] += 1
    return dict(out)


async def interview_rounds(
    account_ids: list[PydanticObjectId], start: date, end: date,
) -> dict[tuple[str, date], list[int]]:
    """(account id, local day) -> [self rounds, caller rounds, self-applied, bidder-applied, unset-applied] held that
    day, by the interview's profile: who took each round, and who applied for the job (Interview.appliedBy)."""
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
    out: dict = defaultdict(lambda: [0, 0, 0, 0, 0])
    for iv in ivs:
        applied = _APPLIED.get(iv.appliedBy.kind if iv.appliedBy else "", 4)
        for day, _group, is_caller, _stage in split_rounds_by_day(iv, first, last, tz):
            counts = out[(str(iv.accountId), day)]
            counts[int(is_caller)] += 1
            counts[applied] += 1
    return dict(out)


async def _dated_scores(account_ids: list[str], start: date, end: date) -> list[tuple[str, date, str, Optional[int]]]:
    """(accountId, local day, "tailored" | "match", score or None) over [start, end].

    "tailored": every tailored resume generated for the profile that day (Resume Generator and Job Applies, applied
    with or not), its score None when it was never scored. "match": the jobs applied to with an uploaded resume
    (resumeId, the score kept on its log entry) or a fixed-resume fork (an uploaded resume copied for the job);
    unscored ones are skipped."""
    tz = app_tz()
    first, last = local_day_start_utc(start, tz), local_day_start_utc(end + timedelta(days=1), tz)
    out = []
    oids = [PydanticObjectId(a) for a in account_ids if PydanticObjectId.is_valid(a)]
    tailored = ResumeApplication.get_motor_collection().find(
        {"accountId": {"$in": oids}, "status": "completed", "fixedFrom": None,
         "createdAt": {"$gte": first, "$lt": last}},
        {"accountId": 1, "createdAt": 1, "matchScore": 1},
    )
    async for d in tailored:
        match = as_match(d.get("matchScore"))  # a bare number on old records
        total = match.get("total") if isinstance(match, dict) else None
        day = d["createdAt"].replace(tzinfo=timezone.utc).astimezone(tz).date()
        out.append((str(d["accountId"]), day, "tailored", int(total) if total is not None else None))
    # Only the fields scored: an entry also keeps its job description.
    cursor = JobApplicationLog.get_motor_collection().find(
        {"accountId": {"$in": account_ids}, "appliedAt": {"$gte": first, "$lt": last}},
        {"accountId": 1, "appliedAt": 1, "resumeId": 1, "tailoredJobId": 1, "score.total": 1},
    )
    entries = [e async for e in cursor]
    ids = [PydanticObjectId(t) for e in entries if (t := e.get("tailoredJobId")) and PydanticObjectId.is_valid(t)]
    forks = {}
    if ids:
        cursor = ResumeApplication.get_motor_collection().find(
            {"_id": {"$in": ids}, "fixedFrom": {"$ne": None}}, {"matchScore": 1},
        )
        forks = {d["_id"]: d async for d in cursor}
    for e in entries:
        tailored_id = e.get("tailoredJobId")
        if tailored_id:  # a tailored resume is counted above, when it was made; only forks are applies here
            doc = forks.get(PydanticObjectId(tailored_id)) if PydanticObjectId.is_valid(tailored_id) else None
            match = as_match((doc or {}).get("matchScore"))
            total = match.get("total") if isinstance(match, dict) else None
        elif e.get("resumeId") and e.get("score"):
            total = e["score"].get("total")
        else:
            continue
        if total is not None:
            day = e["appliedAt"].replace(tzinfo=timezone.utc).astimezone(tz).date()
            out.append((e["accountId"], day, "match", int(total)))
    return out


def _score_lists(entries, first: date, last: Optional[date] = None) -> dict:
    """{"tailored": scores, "match": scores, "tailoredResumes": count} of the (day, kind, score) entries in
    [first, last]; an unscored tailored resume counts toward tailoredResumes only."""
    out = {"tailored": [], "match": [], "tailoredResumes": 0}
    for day, kind, total in entries:
        if day < first or (last is not None and day > last):
            continue
        if kind == "tailored":
            out["tailoredResumes"] += 1
        if total is not None:
            out[kind].append(total)
    return out


async def scores(account_ids: list[str], start: date, end: date) -> dict[str, dict]:
    """accountId -> {"tailored": [...], "match": [...], "tailoredResumes": n} over [start, end]."""
    dated: dict = defaultdict(list)
    for account_id, day, kind, total in await _dated_scores(account_ids, start, end):
        dated[account_id].append((day, kind, total))
    return {a: _score_lists(dated.get(a, ()), start, end) for a in account_ids}


def _per_week(total: int, n_weeks: int) -> Optional[float]:
    return round(total / n_weeks, 1) if n_weeks else None


def _score(values: list[int]) -> dict:
    return {"avg": round(sum(values) / len(values), 1) if values else None, "n": len(values)}


def weekly_from_daily(daily: dict[date, dict]) -> dict[date, dict]:
    """Monday -> the counts of its days, so the Day and Week views always add up to the same totals."""
    out: dict = {}
    for day, counts in daily.items():
        week = out.setdefault(_monday(day), dict.fromkeys(_COUNTS, 0))
        for k in _COUNTS:
            week[k] += counts.get(k, 0)
    return out


def summarize(account_id: str, start: date, n_weeks: int, counts: dict, score_lists: dict) -> dict:
    """One profile's row. `counts`: day (or Monday) -> {selfBids, bidderBids, selfInterviews, callerInterviews},
    already clipped to its span. No weeks measured: nothing per week and no rate."""
    total = {k: sum(c.get(k, 0) for c in counts.values()) for k in _COUNTS}
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
        "tailoredResumes": score_lists.get("tailoredResumes", 0),
        # Totals over the span (All time shows these instead of the per-week values).
        "bids": bids,
        "selfBids": total["selfBids"],
        "bidderBids": total["bidderBids"],
        "interviews": interviews,
        "selfInterviews": total["selfInterviews"],
        "callerInterviews": total["callerInterviews"],
        "selfAppliedInterviews": total["selfAppliedInterviews"],
        "bidderAppliedInterviews": total["bidderAppliedInterviews"],
        "unsetAppliedInterviews": total["unsetAppliedInterviews"],
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
        "callerInterviewsPerWeek", "bids", "selfBids", "bidderBids", "interviews", "selfInterviews",
        "callerInterviews", "tailoredResumes",
    )}
    out["interviewsPerBid"] = mean("interviewsPerBid", 4)
    out["tailorScore"], out["matchScore"] = score("tailorScore"), score("matchScore")
    return out


def _by_account(counts: dict[tuple[str, date], object]) -> dict[str, dict[date, object]]:
    """(account id, day) -> v regrouped as account id -> {day: v}, so each profile reads only its own."""
    out: dict = defaultdict(dict)
    for (key, day), v in counts.items():
        out[key][day] = v
    return out


async def collect(
    accounts: list[Account | ProfileSpan], weeks: int, today: date,
) -> dict[str, tuple[date, int, dict, dict]]:
    """accountId -> (start Monday, weeks measured, daily counts, score lists) over each profile's own span; every
    source (scores too) is loaded once over the widest of them, grouped by profile and clipped to each one's span.
    The daily counts hold every day of the span, zeros included."""
    tz, since = app_tz(), await since_date()
    spans = {}
    for a in accounts:
        created = a.createdAt.replace(tzinfo=timezone.utc).astimezone(tz).date()
        start, end, n = span(weeks, today, since or today, created)
        spans[str(a.id)] = (start, end, n if since else 0)
    out = {k: (s, n, {}, _score_lists((), s)) for k, (s, _e, n) in spans.items()}
    live = [a for a in accounts if spans[str(a.id)][2]]
    if not live:
        return out
    start, end = min(spans[str(a.id)][0] for a in live), max(spans[str(a.id)][1] for a in live)
    sources = await _load(live, start, end)
    for a in live:
        first, last, n = spans[str(a.id)]
        out[str(a.id)] = (first, n, *_days(str(a.id), first, last, sources))
    return out


async def collect_range(
    accounts: list[Account | ProfileSpan], start: date, end: date,
) -> dict[str, tuple[date, int, dict, dict]]:
    """accountId -> (start, weeks the range touches, daily counts, score lists) over the days [start, end], every
    day present (zeros too). Unlike `collect` nothing is clamped: each source counts on its own days (self bids exist
    only once they are typed per profile)."""
    days = (end - start).days + 1
    if days <= 0 or not accounts:
        return {str(a.id): (start, 0, {}, _score_lists((), start)) for a in accounts}
    sources = await _load(accounts, start, end)
    return {str(a.id): (start, -(-days // 7), *_days(str(a.id), start, end, sources)) for a in accounts}


async def _load(accounts: list[Account | ProfileSpan], start: date, end: date) -> tuple[dict, dict, dict]:
    """Every source over [start, end] for these profiles, one query each, grouped by profile: (bids by kind,
    interview rounds, dated scores)."""
    keys, ids = {str(a.id) for a in accounts}, [a.id for a in accounts]
    bids = {
        "selfBids": _by_account(await self_bids(list({a.createdBy for a in accounts}), keys, start, end)),
        "bidderBids": _by_account(await bidder_bids(ids, start, end, schedule_from_settings())),
    }
    rounds = _by_account(await interview_rounds(ids, start, end))
    dated: dict = defaultdict(list)
    for k, day, kind, total in await _dated_scores(list(keys), start, end):
        dated[k].append((day, kind, total))
    return bids, rounds, dated


def _days(key: str, first: date, last: date, sources: tuple[dict, dict, dict]) -> tuple[dict, dict]:
    """One profile's (daily counts for every day of [first, last], score lists) out of the loaded sources."""
    bids, rounds, dated = sources
    daily = {first + timedelta(days=i): dict.fromkeys(_COUNTS, 0) for i in range((last - first).days + 1)}
    for name, counts in bids.items():
        for day, v in counts.get(key, {}).items():
            if day in daily:
                daily[day][name] = v
    for day, held in rounds.get(key, {}).items():
        if day in daily:
            for name, n in zip(_COUNTS[2:], held):
                daily[day][name] = n
    return daily, _score_lists(dated.get(key, ()), first, last)


async def _lifetime_self_bids(owner_ids: list, account_ids: set[str], since: Optional[date]) -> dict[str, int]:
    """account id -> self bids typed per profile on logged days from the launch day on (none before launch). Reads
    only the per-profile map of each plan."""
    if since is None:
        return {}
    cursor = DailyPlan.get_motor_collection().find(
        {"userId": {"$in": owner_ids}, "date": {"$gte": _midnight(since)}, "loggedAt": {"$ne": None}},
        {"done.bidsSelfByProfile": 1},
    )
    out: dict = defaultdict(int)
    async for d in cursor:
        for account_id, n in ((d.get("done") or {}).get("bidsSelfByProfile") or {}).items():
            if account_id in account_ids:
                out[account_id] += n
    return dict(out)


async def _lifetime_bidder_bids(account_ids: list[PydanticObjectId]) -> dict[str, int]:
    """account id -> every approved and pending bid of the profile's bidders (archived ones too), counted by the
    database rather than loaded."""
    profile_of = {b.id: str(b.profileId) async for b in Bidder.find({"profileId": {"$in": account_ids}})}
    pipeline = [
        {"$match": {"bidderId": {"$in": list(profile_of)}, "status": {"$in": ["approved", "pending"]}}},
        {"$group": {"_id": "$bidderId", "n": {"$sum": 1}}},
    ]
    out: dict = defaultdict(int)
    for g in await Bid.get_motor_collection().aggregate(pipeline).to_list(None):
        out[profile_of[g["_id"]]] += g["n"]
    return dict(out)


async def lifetime(accounts: list[Account | ProfileSpan], today: date) -> dict[str, tuple[date, int, dict, dict]]:
    """accountId -> (creation day, weeks since then, {today: totals}, score lists), shaped like `collect` so
    `summarize` reads it: every bid, interview round and scored application since each profile was created, through
    today (this week too). Self bids count from the launch day on: they weren't typed per profile before."""
    if not accounts:
        return {}
    tz = app_tz()
    created = {str(a.id): a.createdAt.replace(tzinfo=timezone.utc).astimezone(tz).date() for a in accounts}
    keys, ids, first = set(created), [a.id for a in accounts], min(created.values())
    selfs = await _lifetime_self_bids(list({a.createdBy for a in accounts}), keys, await since_date())
    bidder = await _lifetime_bidder_bids(ids)
    rounds = _by_account(await interview_rounds(ids, first, today))
    dated: dict = defaultdict(list)
    for k, day, kind, total in await _dated_scores(list(keys), first, today):
        dated[k].append((day, kind, total))
    out = {}
    for key, day0 in created.items():
        held = [v for day, v in rounds.get(key, {}).items() if day >= day0]
        totals = {
            "selfBids": selfs.get(key, 0), "bidderBids": bidder.get(key, 0),
            **{name: sum(v[i] for v in held) for i, name in enumerate(_COUNTS[2:])},
        }
        out[key] = (day0, (today - day0).days // 7 + 1, {today: totals}, _score_lists(dated.get(key, ()), day0))
    return out
