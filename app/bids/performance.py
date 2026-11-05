"""Bids → Performance tab: how each bidder's bids add up over a week, month or custom range, on Bid Track days (see
the bidder performance spec §1).

Pure helpers, no database: the weekly columns, the 50+ day streaks, the previous period to compare with and the top
performer cards. A 50+ day is a weekday (Monday–Friday) with TARGET or more approved + pending bids; weekends neither
count nor break a streak.
"""
from __future__ import annotations

from calendar import monthrange
from datetime import date, timedelta

TARGET = 50  # bids a weekday needs to be a 50+ day
MIN_BIDS_FOR_RATE = 100  # fewer bids than this: the bid→interview rate is too noisy for "Best conversion"


def week_columns(start: date, end: date) -> list[dict]:
    """Every Monday–Sunday week touching [start, end], clipped to it; `partial` when the range cuts the week."""
    out, s = [], start
    while s <= end:
        mon = s - timedelta(days=s.weekday())
        e = min(mon + timedelta(days=6), end)
        out.append({"start": s.isoformat(), "end": e.isoformat(), "partial": s != mon or e != mon + timedelta(days=6)})
        s = e + timedelta(days=1)
    return out


def previous_range(period: str, start: date, end: date) -> tuple[date, date]:
    """The period to compare with: the week before; the month before, cut to the same number of days (clipped to its
    last day); a custom range's length of days just before it."""
    days = (end - start).days + 1
    if period == "week":
        return start - timedelta(days=7), end - timedelta(days=7)
    if period == "month":
        first = (start.replace(day=1) - timedelta(days=1)).replace(day=1)
        return first, min(first + timedelta(days=days - 1), date(first.year, first.month, monthrange(first.year, first.month)[1]))
    return start - timedelta(days=days), start - timedelta(days=1)


def streaks(counts: dict[date, int], first: date, last: date, today: date) -> dict:
    """Longest and current run of 50+ weekdays, hit days and weekdays counted over [first, last]. Today only counts
    once it has hit the target: it isn't over, so it can't miss yet."""
    stop = min(last, today - timedelta(days=1))
    days = [first + timedelta(days=i) for i in range((stop - first).days + 1)]
    if first <= today <= last and counts.get(today, 0) >= TARGET:
        days.append(today)
    longest = run = hits = n = 0
    for d in days:
        if d.weekday() >= 5:
            continue
        n += 1
        if counts.get(d, 0) >= TARGET:
            hits += 1
            run += 1
            longest = max(longest, run)
        else:
            run = 0
    return {"longestStreak": longest, "currentStreak": run, "hitDays": hits, "weekdays": n}


def rank(rows: list[dict]) -> dict:
    """Top 3 bidder ids for each card; a row with 0 or no value for a card isn't on it."""
    name = lambda r: r["name"].lower()

    def top(key, ties, pool=rows):
        have = [r for r in pool if r.get(key)]
        have.sort(key=lambda r: (-r[key], *ties(r)))
        return [r["id"] for r in have[:3]]

    return {
        "bids": top("total", lambda r: (-r["approved"], name(r))),
        "streak": top("longestStreak", lambda r: (-(r["hitRate"] or 0), -r["total"])),
        "interviews": top("interviews", lambda r: (-(r["per100"] or 0), name(r))),
        "conversion": top("per100", lambda r: (-r["interviews"], name(r)),
                          [r for r in rows if r["total"] >= MIN_BIDS_FOR_RATE]),
    }
