"""One-off: recount one stored daily or weekly bid report under the current schedule and edit its Slack post in place.

Used after a schedule change moved a period's window once its report was out (e.g. the switch at 2026-11-15T20:00:
the daily report for Mon 2026-11-16 was stored with the old 18:30 cutoff; under the current schedule that day ends
at 18:00).

1. The period's window comes from the CURRENT settings schedule (schedule_from_settings), and its rows are counted the
   way the report cron does (app.bids.reports.build_rows): every active bidder, plus any bidder already on the stored
   report, so nobody silently drops off it.
2. Prints, per bidder, the stored count/pending/rejected next to the recomputed ones, and the old and new windows.
3. --apply replaces the stored rows of that kind and period key with the recomputed ones.
4. --apply then re-renders the post (app.bids.reports._render, My bids included) and edits the stored BidReportPost's
   messages in place with chat.update (app.bids.reports.update_report), then marks the post synced. It never posts a
   new message: if the report would now need more messages than were posted, it stops before editing.
5. No stored post: says so and exits without posting (the rows are still replaced with --apply).

A period not yet closed (its window ends after now) is refused. Dry run by default. Safe to run twice: a second run
recomputes the same rows, replaces them again and edits the same post again.

Run:
    cd engineers_backend
    .venv/Scripts/python -m scripts.rerender_bid_report --kind daily --period-key 2026-11-16           # dry run
    .venv/Scripts/python -m scripts.rerender_bid_report --kind daily --period-key 2026-11-16 --apply   # write + edit

Set the schedule variables (BID_SCHEDULE_SWITCH_AT, BID_NEXT_*, BID_SCHEDULE_SHORT_WEEK, BID_RECORDS_START) to what
production runs, and SLACK_BOT_TOKEN for --apply. Connects with MONGODB_URI and registers the models only: no startup
migrations or seeds, no index creation.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional

from app.config import get_settings


def _fmt(dt: datetime, tz) -> str:
    if dt.tzinfo is None:  # stored datetimes come back naive UTC
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz).strftime("%Y-%m-%d %H:%M")


def _triple(row) -> str:
    if row is None:
        return "-"
    return "/".join("?" if v is None else str(v) for v in (row.count, row.pending, row.rejected))


def _cutoff_for(kind: str, period_key: str, sched):
    """The `kind` cutoff whose local date is `period_key`, or None when that day has none (e.g. a weekly key that is
    not a weekly cutoff day)."""
    from app.bids.windows import next_cutoff, period_key as key_of

    day = date.fromisoformat(period_key)
    midnight = datetime.combine(day, time(0), tzinfo=sched.tz)
    cutoff = next_cutoff(kind, midnight - timedelta(microseconds=1), sched)
    return cutoff if key_of(cutoff) == period_key else None


async def rerender(kind: str, period_key: str, *, apply: bool, now: Optional[datetime] = None, storage=None) -> int:
    from app.bids.reports import BeanieReportStore, _render, build_rows, update_report
    from app.bids.storage import get_bid_storage
    from app.bids.windows import schedule_from_settings, window_for
    from app.models.bid_report import BidReport
    from app.models.bid_report_post import BidReportPost
    from app.models.bidder import Bidder

    now = now or datetime.now(timezone.utc)
    sched = schedule_from_settings()
    store = BeanieReportStore()
    try:
        cutoff = _cutoff_for(kind, period_key, sched)
    except ValueError:
        print(f"ERROR: --period-key {period_key!r} is not a YYYY-MM-DD date")
        return 1
    if cutoff is None:
        print(f"ERROR: there is no {kind} cutoff on {period_key} under the current schedule")
        return 1
    start, end = window_for(kind, cutoff, sched)
    if end > now:
        print(f"ERROR: the {kind} period {period_key} is not closed yet (it ends {_fmt(end, sched.tz)}); nothing to fix")
        return 1
    if kind == "weekly":
        other = await store.overlapping_weekly(start, end, period_key)
        if other:
            print(f"ERROR: the recomputed weekly window overlaps the stored weekly report {other}; refusing")
            return 1

    stored = await store.load_rows(kind, period_key)
    bidders = await store.active_bidders()
    have = {b.id for b in bidders}
    missing = [r.bidderId for r in stored if r.bidderId not in have]
    if missing:  # on the stored report but archived since: keep them on it
        bidders += await Bidder.find({"_id": {"$in": missing}}).to_list()
    names = await store.profile_names([b.profileId for b in bidders if b.profileId])
    rows = await build_rows(kind, start, end, period_key, bidders, names, storage or get_bid_storage(), sched)

    print(f"Report: {kind} {period_key} ({sched.tz.key})")
    if stored:
        print(f"Old window: {_fmt(stored[0].periodStart, sched.tz)} -> {_fmt(stored[0].periodEnd, sched.tz)}")
    else:
        print("Old window: (no stored rows)")
    print(f"New window: {_fmt(start, sched.tz)} -> {_fmt(end, sched.tz)}")
    print(f"{'bidder':<24} {'stored c/p/r':>12}    {'new c/p/r':>10}")
    by_bidder = {r.bidderId: r for r in stored}
    for r in sorted(rows, key=lambda r: r.bidderName.lower()):
        old = by_bidder.get(r.bidderId)
        changed = old is None or (old.count, old.pending, old.rejected) != (r.count, r.pending, r.rejected)
        print(f"{r.bidderName:<24} {_triple(old):>12} -> {_triple(r):>10}{' *' if changed else ''}")
    errors = [r for r in rows if r.error]
    if errors:
        print(f"WARNING: {len(errors)} row(s) could not be counted: " + ", ".join(f"{r.bidderName} ({r.error})" for r in errors))

    post = await BidReportPost.find_one(BidReportPost.kind == kind, BidReportPost.periodKey == period_key)
    if not apply:
        print(f"Post: {'channel ' + post.channel + ' ts ' + ','.join(post.ts) if post else 'no stored Slack post'}")
        print("DRY RUN: nothing written. Re-run with --apply to replace the rows and edit the post.")
        return 0

    await BidReport.find(BidReport.kind == kind, BidReport.periodKey == period_key).delete()
    if rows:
        await BidReport.insert_many([BidReport(**r.model_dump()) for r in rows])
    print(f"APPLIED: replaced {len(stored)} stored row(s) with {len(rows)} recomputed row(s)")

    if post is None:
        print("Post: no stored Slack post for this report; nothing edited, nothing posted.")
        return 0
    if not (get_settings().SLACK_BOT_TOKEN or "").strip():
        print("ERROR: SLACK_BOT_TOKEN is not set; rows replaced but the post was not edited")
        return 1
    text, messages = await _render(kind, start, end, rows, store, sched, now)
    if len(messages) > len(post.ts):
        print(f"ERROR: the report now needs {len(messages)} message(s) but {len(post.ts)} were posted; editing would "
              "post a new reply, so the post was not edited")
        return 1
    ts = await update_report(post.channel, post.ts, text, messages)
    await store.mark_synced(post, ts)
    print(f"EDITED: Slack post {post.channel} {','.join(ts)} in place")
    return 0


async def _connect():
    """Mongo + the models this script reads and writes. Deliberately not app.db.init_db (it writes migrations/seeds)."""
    from beanie import init_beanie
    from motor.motor_asyncio import AsyncIOMotorClient

    from app.models import Account, UserExtra
    from app.models.bid import Bid
    from app.models.bid_report import BidReport
    from app.models.bid_report_post import BidReportPost
    from app.models.bidder import Bidder

    client = AsyncIOMotorClient(get_settings().MONGODB_URI)
    await init_beanie(
        database=client.get_default_database(),
        document_models=[UserExtra, Account, Bidder, Bid, BidReport, BidReportPost],
        skip_indexes=True,
    )
    return client


async def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--kind", choices=("daily", "weekly"), required=True)
    parser.add_argument("--period-key", required=True, help="YYYY-MM-DD: the report's local cutoff date")
    parser.add_argument("--apply", action="store_true", help="replace the rows and edit the post (default: dry run)")
    args = parser.parse_args()
    client = await _connect()
    try:
        return await rerender(args.kind, args.period_key, apply=args.apply)
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
