"""Daily and weekly bid reports: build rows from storage counts, post to Slack, de-dup on the cron.

Called from the 5-minute cron (app/alerts_tick.py) and the HTTP fallback (app/routers/bidders.py).
A period is reported at most once: rows are unique on (kind, periodKey, bidderId) and the post is
recorded in SlackAlertLog under `bids-<kind>:<periodKey>`. Where the post lives is kept in BidReportPost;
a review that recounts a row marks it dirty and a later tick edits the post in place.
"""

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional, Protocol

from beanie import PydanticObjectId
from pymongo.errors import BulkWriteError, DuplicateKeyError

from app.bids.records import count_by_status, uses_records
from app.bids.slack_format import Owner, format_report
from app.bids.storage import BidStorage, count_folders, get_bid_storage
from app.bids.windows import BidSchedule, Kind, latest_cutoff, period_key, schedule_from_settings, window_for
from app.config import get_settings
from app.models import Account, UserExtra
from app.models.bid_report import BidReport, BidReportRow
from app.models.bid_report_post import BidReportPost
from app.models.bidder import Bidder
from app.models.slack_alert_log import SlackAlertLog
from app.slack_bot import _MESSAGE_GONE, CALLER_CHANNEL_SENTINEL, _mention, _slack_api

log = logging.getLogger("bids.reports")

CATCH_UP = timedelta(hours=6)
KINDS: tuple[Kind, ...] = ("daily", "weekly")


def report_key(kind: Kind, period_key: str) -> str:
    return f"bids-{kind}:{period_key}"


class ReportStore(Protocol):
    async def claim(self, key: str) -> bool: ...
    async def release(self, key: str) -> None: ...
    async def load_rows(self, kind: Kind, period_key: str) -> list[BidReportRow]: ...
    async def save_rows(self, rows: list[BidReportRow]) -> list[BidReportRow]: ...
    async def overlapping_weekly(self, start: datetime, end: datetime, exclude_key: str) -> Optional[str]: ...
    async def active_bidders(self) -> list: ...
    async def profile_names(self, ids: list[PydanticObjectId]) -> dict[str, str]: ...
    async def owners(self, ids: list[PydanticObjectId]) -> dict[str, Owner]: ...
    async def save_post(self, kind: Kind, period_key: str, channel: str, ts: list[str]) -> None: ...
    async def dirty_posts(self) -> list: ...
    async def mark_synced(self, post, ts: list[str]) -> None: ...
    async def drop_post(self, post) -> None: ...


def _only_duplicates(e: BulkWriteError) -> bool:
    errors = (e.details or {}).get("writeErrors") or []
    return bool(errors) and all(err.get("code") == 11000 for err in errors)


class BeanieReportStore:
    async def claim(self, key: str) -> bool:
        """Insert the de-dup key; False when another run already holds it."""
        try:
            await SlackAlertLog(
                userId=CALLER_CHANNEL_SENTINEL, kind=key, sentAt=datetime.now(timezone.utc),
            ).insert()
            return True
        except DuplicateKeyError:
            return False

    async def release(self, key: str) -> None:
        await SlackAlertLog.find(
            SlackAlertLog.userId == CALLER_CHANNEL_SENTINEL, SlackAlertLog.kind == key,
        ).delete()

    async def load_rows(self, kind: Kind, period_key: str) -> list[BidReportRow]:
        return await BidReport.find(BidReport.kind == kind, BidReport.periodKey == period_key).to_list()

    async def save_rows(self, rows: list[BidReportRow]) -> list[BidReportRow]:
        if not rows:
            return []
        try:
            await BidReport.insert_many([BidReport(**r.model_dump()) for r in rows])
            return rows
        except DuplicateKeyError:
            pass
        except BulkWriteError as e:
            if not _only_duplicates(e):
                raise
        # Another run inserted this period first; use its rows.
        return await self.load_rows(rows[0].kind, rows[0].periodKey)

    async def overlapping_weekly(self, start: datetime, end: datetime, exclude_key: str) -> Optional[str]:
        """periodKey of a stored weekly report, other than `exclude_key`, whose [periodStart, periodEnd) overlaps [start, end)."""
        row = await BidReport.find_one(
            BidReport.kind == "weekly", BidReport.periodKey != exclude_key,
            BidReport.periodStart < end, BidReport.periodEnd > start,
        )
        return row.periodKey if row else None

    async def active_bidders(self) -> list:
        return await Bidder.find(Bidder.archivedAt == None).to_list()  # noqa: E711

    async def profile_names(self, ids: list[PydanticObjectId]) -> dict[str, str]:
        if not ids:
            return {}
        accounts = await Account.find({"_id": {"$in": ids}}).to_list()
        return {str(a.id): a.name for a in accounts}

    async def owners(self, ids: list[PydanticObjectId]) -> dict[str, Owner]:
        if not ids:
            return {}
        users = await UserExtra.find({"_id": {"$in": ids}}).to_list()
        return {str(u.id): Owner(name=u.name or u.email or "", label=_mention(u)) for u in users}

    async def save_post(self, kind: Kind, period_key: str, channel: str, ts: list[str]) -> None:
        await BidReportPost.get_motor_collection().update_one(
            {"kind": kind, "periodKey": period_key},
            {"$set": {"channel": channel, "ts": ts, "dirty": False, "updatedAt": datetime.utcnow()},
             "$setOnInsert": {"rev": 0, "createdAt": datetime.utcnow()}},
            upsert=True,
        )

    async def dirty_posts(self) -> list[BidReportPost]:
        return await BidReportPost.find(BidReportPost.dirty == True).to_list()  # noqa: E712

    async def mark_synced(self, post: BidReportPost, ts: list[str]) -> None:
        """Save the edited post's ts; clear dirty only if no review bumped rev since `post` was read."""
        coll, now = BidReportPost.get_motor_collection(), datetime.utcnow()
        await coll.update_one({"_id": post.id}, {"$set": {"ts": ts, "updatedAt": now}})
        await coll.update_one({"_id": post.id, "rev": post.rev}, {"$set": {"dirty": False}})

    async def drop_post(self, post: BidReportPost) -> None:
        await post.delete()


async def build_rows(
    kind: Kind,
    start: datetime,
    end: datetime,
    period_key: str,
    bidders: list,
    profile_names: dict[str, str],
    storage: BidStorage,
    sched: BidSchedule,
) -> list[BidReportRow]:
    """One row per bidder. A window that starts on or after BID_RECORDS_START counts approved bid
    records (pending and rejected reported beside them); an earlier one counts screenshot folders."""
    if uses_records(start, sched):
        by_status = await count_by_status([b.id for b in bidders], start, end)
        counts = {str(b.id): (by_status[str(b.id)][0], None) for b in bidders}
    else:
        by_status = None
        counts = await count_folders([(str(b.id), b.screenshotFolderUrl) for b in bidders], start, end, storage)
    rows = []
    for b in bidders:
        count, error = counts[str(b.id)]
        _, pending, rejected = by_status[str(b.id)] if by_status else (None, None, None)
        rows.append(BidReportRow(
            kind=kind, periodKey=period_key, periodStart=start, periodEnd=end,
            bidderId=b.id, ownerId=b.createdBy, bidderName=b.name, country=b.country,
            profileName=profile_names.get(str(b.profileId)) if b.profileId else None,
            rate=b.rate, count=count,
            amount=None if count is None else round(count * b.rate, 2),
            error=error, pending=pending, rejected=rejected,
        ))
    return rows


def bids_slack_configured() -> bool:
    s = get_settings()
    return bool((s.SLACK_BIDS_CHANNEL_ID or "").strip() and (s.SLACK_BOT_TOKEN or "").strip())


_FLAGS = {"unfurl_links": False, "unfurl_media": False}


async def post_report(text: str, messages: list[list[dict]]) -> tuple[str, list[str]]:
    """Post messages[0] to the bids channel and the rest as replies in its thread. Raises on failure.

    Returns the channel and the ts of every message, so the report can be edited later."""
    channel = (get_settings().SLACK_BIDS_CHANNEL_ID or "").strip()
    first = await _slack_api(
        "chat.postMessage", {"channel": channel, "text": text, "blocks": messages[0], **_FLAGS},
    )
    ts = [first["ts"]]
    for blocks in messages[1:]:
        reply = await _slack_api(
            "chat.postMessage",
            {"channel": channel, "thread_ts": ts[0], "text": text, "blocks": blocks, **_FLAGS},
        )
        ts.append(reply["ts"])
    return first.get("channel") or channel, ts


async def update_report(channel: str, ts: list[str], text: str, messages: list[list[dict]]) -> list[str]:
    """Edit a posted report in place and return the ts of its messages now. Nothing new is said in the thread:
    only when the report grew past a message (50 blocks) is the extra part posted there, as on first post."""
    for t, blocks in zip(ts, messages):
        await _slack_api("chat.update", {"channel": channel, "ts": t, "text": text, "blocks": blocks})
    kept = ts[:len(messages)]
    for blocks in messages[len(ts):]:
        reply = await _slack_api(
            "chat.postMessage",
            {"channel": channel, "thread_ts": ts[0], "text": text, "blocks": blocks, **_FLAGS},
        )
        kept.append(reply["ts"])
    for t in ts[len(messages):]:
        await _slack_api("chat.delete", {"channel": channel, "ts": t})
    return kept


async def _render(
    kind: Kind, start: datetime, end: datetime, rows: list[BidReportRow], store: ReportStore, sched: BidSchedule,
    updated_at: Optional[datetime] = None,
) -> tuple[str, list[list[dict]]]:
    owners = await store.owners(list({r.ownerId for r in rows}))
    return format_report(
        kind, start, end, rows, owners, sched.tz, get_settings().FRONTEND_ORIGIN.rstrip("/"), updated_at,
    )


def _utc(dt: datetime) -> datetime:
    """Stored datetimes come back naive UTC."""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def refresh_posts(now: datetime, sched: BidSchedule, store: ReportStore, edit) -> int:
    """Edit every posted report whose rows a review recounted since; returns how many were edited.

    A failed edit is left dirty for the next tick; a report deleted in Slack is forgotten."""
    edited = 0
    for post in await store.dirty_posts():
        try:
            rows = await store.load_rows(post.kind, post.periodKey)
            if not rows:
                await store.drop_post(post)
                continue
            text, messages = await _render(
                post.kind, _utc(rows[0].periodStart), _utc(rows[0].periodEnd), rows, store, sched, now,
            )
            ts = await edit(post.channel, post.ts, text, messages)
        except Exception as exc:
            if isinstance(exc, RuntimeError) and str(exc) in _MESSAGE_GONE:
                log.info("bid %s report %s gone from Slack (%s)", post.kind, post.periodKey, exc)
                await store.drop_post(post)
            else:
                log.warning("bid %s report edit failed for %s: %s", post.kind, post.periodKey, exc)
            continue
        await store.mark_synced(post, ts)
        edited += 1
    return edited


async def _run_kind(
    kind: Kind, now: datetime, sched: BidSchedule, store: ReportStore, storage: BidStorage, post: Optional[Any],
) -> str:
    cutoff = latest_cutoff(kind, now, sched)
    if now - cutoff >= CATCH_UP:
        return "not-due"
    pk = period_key(cutoff)
    key = report_key(kind, pk)
    # Claim before doing anything so an overlapping tick (cron + HTTP fallback) cannot also post.
    if not await store.claim(key):
        return "done"
    try:
        start, end = window_for(kind, cutoff, sched)
        if kind == "weekly":
            # A switch deployed late (old code posted the Saturday week) would pay those days twice. Refuse, and keep
            # the claim: later ticks skip this period until someone intervenes (e.g. moves BID_SCHEDULE_SWITCH_AT).
            other = await store.overlapping_weekly(start, end, pk)
            if other:
                log.error("bid weekly report %s not posted: its window overlaps the stored weekly report %s", pk, other)
                return f"overlap:{other}"
        rows = await store.load_rows(kind, pk)
        if not rows:
            bidders = await store.active_bidders()
            if not bidders:
                return "no-bidders"  # keep the claim so later ticks skip this period
            names = await store.profile_names([b.profileId for b in bidders if b.profileId])
            rows = await store.save_rows(await build_rows(kind, start, end, pk, bidders, names, storage, sched))
        if post is None:
            await store.release(key)
            return "saved:slack-not-configured"
        text, messages = await _render(kind, start, end, rows, store, sched)
    except Exception:
        await store.release(key)  # let the next tick retry
        raise
    try:
        channel, ts = await post(text, messages)
    except Exception as exc:
        await store.release(key)
        log.warning("bid %s report post failed for %s: %s", kind, pk, exc)
        return "failed:post"
    try:
        await store.save_post(kind, pk, channel, ts)
    except Exception as exc:  # the report is out and the claim stands; it just won't be edited after reviews
        log.warning("bid %s report %s posted but not tracked: %s", kind, pk, exc)
    return "posted"


async def run_bid_reports_tick(
    now: Optional[datetime] = None,
    *,
    store: Optional[ReportStore] = None,
    storage: Optional[BidStorage] = None,
    post=None,
    edit=None,
    sched: Optional[BidSchedule] = None,
) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    sched = sched or schedule_from_settings()
    store = store or BeanieReportStore()
    storage = storage or get_bid_storage()
    if post is None and bids_slack_configured():
        post = post_report
    result: dict[str, Any] = {}
    for kind in KINDS:
        result[kind] = await _run_kind(kind, now, sched, store, storage, post)
        log.info("bid %s report: %s", kind, result[kind])
    if post is not None:  # Slack is configured (or a test posts)
        result["refreshed"] = await refresh_posts(now, sched, store, edit or update_report)
        log.info("bid reports edited after reviews: %s", result["refreshed"])
    return result
