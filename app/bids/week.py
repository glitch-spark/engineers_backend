"""Pay weeks for the Bidders page: the weekly report window, its seven daily windows, and per-bidder counts.

A pay week is the weekly report window. Its days are the daily windows that start inside it, so the last day
(Saturday with the defaults) ends after the weekly cutoff: day counts show that whole day, while the week's totals
and pay use the pay-week window itself and match the weekly Slack report.
"""

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional

from app.bids.live_counts import TtlCache
from app.bids.records import _naive_utc, count_by_status, uses_records
from app.bids.schemas import _iso_utc, bidder_status
from app.bids.storage import BidStorage, count_folder_windows
from app.bids.windows import BidSchedule, latest_cutoff, period_key, window_for
from app.models import Account, UserExtra
from app.models.bid import Bid
from app.models.bidder import Bidder

Counts = tuple[Optional[int], Optional[int], Optional[int]]


@dataclass
class DayWindow:
    key: str  # period key of the day's end cutoff
    start: datetime  # UTC-aware
    end: datetime


@dataclass
class PayWeek:
    start: datetime  # UTC-aware [start, end)
    end: datetime
    cutoff: datetime  # weekly cutoff in the report timezone (== end)
    days: list[DayWindow]


def pay_week(sched: BidSchedule, now: datetime, week: Optional[date] = None) -> PayWeek:
    """The pay week holding `week` (taken at local noon) or, by default, `now`: the first weekly cutoff strictly
    after that moment ends it. Raises ValueError/OverflowError at the edges of the calendar."""
    anchor = now if week is None else datetime.combine(week, time(12), tzinfo=sched.tz)
    last = latest_cutoff("weekly", anchor, sched)
    cutoff = datetime.combine(last.date() + timedelta(days=7), sched.weekly_at, tzinfo=sched.tz)
    start, end = window_for("weekly", cutoff, sched)
    days = []
    for offset in range(8):  # local dates the week touches; keep the daily windows that start inside it
        day_cutoff = datetime.combine(last.date() + timedelta(days=offset + 1), sched.daily_at, tzinfo=sched.tz)
        d_start, d_end = window_for("daily", day_cutoff, sched)
        if start <= d_start < end:
            days.append(DayWindow(period_key(day_cutoff), d_start, d_end))
    return PayWeek(start, end, cutoff, days)


def _sum_or_none(values) -> Optional[float]:
    """Sum of the values; None when any is unknown (a folder day or a failed listing), since a partial sum would
    pass for the total."""
    values = list(values)
    return None if any(v is None for v in values) else sum(values)


def combine_boards(boards: list[dict]) -> dict:
    """Pay-week money of several consecutive pay weeks: each bidder's `week` block (approved, pending, rejected, pay,
    toReview) and the team totals, as the sum of what `week_board` reports for each pay week, so a period of two pay
    weeks pays exactly what its two pay weeks pay. A sum is None when any of its pay weeks is
    unknown, and the first error is kept (on each row and on the totals)."""
    blocks: dict[str, dict] = {}
    for rows in zip(*(b["bidders"] for b in boards)):
        weeks = [r["week"] for r in rows]
        block = {k: _sum_or_none(w[k] for w in weeks) for k in ("approved", "pending", "rejected", "toReview")}
        pay = _sum_or_none(w["pay"] for w in weeks)
        block["pay"] = None if pay is None else round(pay, 2)
        block["source"] = "records" if all(w["source"] == "records" for w in weeks) else "folders"
        block["error"] = next((w["error"] for w in weeks if w["error"]), None)
        blocks[rows[0]["id"]] = block
    totals = {k: _sum_or_none(b["totals"][k] for b in boards) for k in ("approved", "pending", "rejected", "toReview")}
    pay = _sum_or_none(b["totals"]["pay"] for b in boards)
    totals["pay"] = None if pay is None else round(pay, 2)
    totals["error"] = next((blk["error"] for blk in blocks.values() if blk["error"]), None)
    return {"blocks": blocks, "totals": totals}


def week_label(pw: PayWeek) -> str:
    d = pw.cutoff.date()
    return f"Week ending {d:%a} {d:%b} {d.day}"


async def _day_counts(bidders: list[Bidder], days: list[DayWindow]) -> dict[tuple[str, int], tuple[int, int, int]]:
    """{(bidder id, day index): (approved, pending, rejected)} from one aggregation over the week's days."""
    if not bidders or not days:
        return {}
    branches = [{"case": {"$lt": ["$firstAt", _naive_utc(d.end)]}, "then": i} for i, d in enumerate(days)]
    pipeline = [
        {"$match": {
            "bidderId": {"$in": [b.id for b in bidders]},
            "firstAt": {"$gte": _naive_utc(days[0].start), "$lt": _naive_utc(days[-1].end)},
        }},
        {"$group": {
            "_id": {"bidderId": "$bidderId", "status": "$status", "day": {"$switch": {"branches": branches}}},
            "n": {"$sum": 1},
        }},
    ]
    slot = {"approved": 0, "pending": 1, "rejected": 2}
    out: dict[tuple[str, int], list[int]] = {}
    for g in await Bid.get_motor_collection().aggregate(pipeline).to_list(None):
        key = (str(g["_id"]["bidderId"]), g["_id"]["day"])
        out.setdefault(key, [0, 0, 0])[slot[g["_id"]["status"]]] += g["n"]
    return {k: tuple(v) for k, v in out.items()}


async def _folder_counts(
    bidders: list[Bidder], windows: list[tuple[datetime, datetime]], now: datetime,
    storage: BidStorage, cache: Optional[TtlCache],
) -> dict[str, tuple[Optional[list[int]], Optional[str]]]:
    """Folder counts of every bidder over `windows`, one listing per bidder; successes are cached."""
    if not windows:
        return {}
    found, misses = {}, []
    for b in bidders:
        key = ("week", str(b.id), b.screenshotFolderUrl, tuple(s.isoformat() for s, _ in windows))
        hit = cache.get(key, now) if cache else None
        if hit is None:
            misses.append((str(b.id), b.screenshotFolderUrl))
        else:
            found[str(b.id)] = (hit, None)
    for bidder_id, (counts, error) in (await count_folder_windows(misses, windows, storage)).items():
        found[bidder_id] = (counts, error)
        if counts is not None and cache:
            b = next(b for b in bidders if str(b.id) == bidder_id)
            cache.set(("week", bidder_id, b.screenshotFolderUrl, tuple(s.isoformat() for s, _ in windows)), counts, now)
    return found


def _pay(approved: Optional[int], rate: float) -> Optional[float]:
    return None if approved is None else round(approved * rate, 2)


def _sum(values) -> Optional[int]:
    """Sum of the known values; None when every value is unknown (folder days have no pending/rejected)."""
    values = list(values)
    known = [v for v in values if v is not None]
    return sum(known) if known or not values else None


async def week_board(
    bidders: list[Bidder], pw: PayWeek, now: datetime, sched: BidSchedule, storage: BidStorage,
    cache: Optional[TtlCache] = None,
) -> dict:
    """The /bids/week response: per-bidder day counts and pay-week totals, plus team totals."""
    now = now.astimezone(timezone.utc)
    past = [i for i, d in enumerate(pw.days) if d.start <= now]
    record_days = [i for i in past if uses_records(pw.days[i].start, sched)]
    folder_days = [i for i in past if i not in record_days]
    week_records = uses_records(pw.start, sched)

    counts = await _day_counts(bidders, [pw.days[i] for i in record_days])
    counts = {(b, record_days[j]): c for (b, j), c in counts.items()}
    week_counts = await count_by_status([b.id for b in bidders], pw.start, pw.end) if week_records else {}
    folder_windows = [(pw.days[i].start, pw.days[i].end) for i in folder_days]
    if not week_records and pw.start <= now:
        folder_windows.append((pw.start, pw.end))
    folders = await _folder_counts(bidders, folder_windows, now, storage, cache)

    ids = list({b.profileId for b in bidders if b.profileId})
    profiles = {a.id: a.name for a in await Account.find({"_id": {"$in": ids}}).to_list()} if ids else {}
    owner_ids = list({b.createdBy for b in bidders})
    owners = {u.id: u.name for u in await UserExtra.find({"_id": {"$in": owner_ids}}).to_list()} if owner_ids else {}

    rows = []
    for b in bidders:
        bid_id = str(b.id)
        listed, error = folders.get(bid_id, (None, None))
        days = []
        for i, d in enumerate(pw.days):
            if i in folder_days:
                n = None if listed is None else listed[folder_days.index(i)]
                c, source = (n, None, None), "folders"
            else:
                c = counts.get((bid_id, i), (0, 0, 0))
                source = "records" if uses_records(d.start, sched) else "folders"
            days.append({"day": d.key, "approved": c[0], "pending": c[1], "rejected": c[2], "source": source,
                         "error": error if i in folder_days else None})
        if week_records:
            approved, pending, rejected = week_counts[bid_id]
            week = {"approved": approved, "pending": pending, "rejected": rejected, "pay": _pay(approved, b.rate),
                    "toReview": pending, "source": "records", "error": None}
        else:
            approved = 0 if pw.start > now else (None if listed is None else listed[-1])
            week = {"approved": approved, "pending": None, "rejected": None, "pay": _pay(approved, b.rate),
                    "toReview": None, "source": "folders", "error": error}
        rows.append({
            "id": bid_id, "name": b.name, "status": bidder_status(b), "country": b.country,
            "profileName": profiles.get(b.profileId), "rate": b.rate, "days": days, "week": week,
            "ownerId": str(b.createdBy), "ownerName": owners.get(b.createdBy),
        })

    pays = [r["week"]["pay"] for r in rows if r["week"]["pay"] is not None]
    return {
        "week": {
            "start": _iso_utc(pw.start), "end": _iso_utc(pw.end), "label": week_label(pw), "timezone": sched.tz.key,
            "days": [{"day": d.key, "start": _iso_utc(d.start), "end": _iso_utc(d.end),
                      "isToday": d.start <= now < d.end, "isFuture": d.start > now} for d in pw.days],
        },
        "bidders": rows,
        "totals": {
            **{k: _sum(r["week"][k] for r in rows) for k in ("approved", "pending", "rejected", "toReview")},
            "pay": round(sum(pays), 2) if pays else None,
            "days": [{"day": d.key, **{k: _sum(r["days"][i][k] for r in rows)
                                       for k in ("approved", "pending", "rejected")}}
                     for i, d in enumerate(pw.days)],
        },
    }
