"""Daily and weekly bid reports: build rows from storage counts, post to Slack, de-dup on the cron.

Called from the 5-minute cron (app/alerts_tick.py) and the HTTP fallback (app/routers/bidders.py).
A period is reported at most once: rows are unique on (kind, periodKey, bidderId) and the post is
recorded in SlackAlertLog under `bids-<kind>:<periodKey>`.
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
from app.models.bidder import Bidder
from app.models.slack_alert_log import SlackAlertLog
from app.slack_bot import CALLER_CHANNEL_SENTINEL, _mention, _slack_api

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
    async def active_bidders(self) -> list: ...
    async def profile_names(self, ids: list[PydanticObjectId]) -> dict[str, str]: ...
    async def owners(self, ids: list[PydanticObjectId]) -> dict[str, Owner]: ...


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
    """One row per bidder. A window that ends after BID_RECORDS_START counts approved bid records
    (pending and rejected reported beside them); an earlier one counts screenshot folders."""
    if uses_records(end, sched):
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


async def post_report(text: str, messages: list[list[dict]]) -> None:
    """Post messages[0] to the bids channel and the rest as replies in its thread. Raises on failure."""
    channel = (get_settings().SLACK_BIDS_CHANNEL_ID or "").strip()
    flags = {"unfurl_links": False, "unfurl_media": False}
    first = await _slack_api(
        "chat.postMessage", {"channel": channel, "text": text, "blocks": messages[0], **flags},
    )
    for blocks in messages[1:]:
        await _slack_api(
            "chat.postMessage",
            {"channel": channel, "thread_ts": first["ts"], "text": text, "blocks": blocks, **flags},
        )


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
        owners = await store.owners(list({r.ownerId for r in rows}))
        text, messages = format_report(
            kind, start, end, rows, owners, sched.tz, get_settings().FRONTEND_ORIGIN.rstrip("/"),
        )
    except Exception:
        await store.release(key)  # let the next tick retry
        raise
    try:
        await post(text, messages)
    except Exception as exc:
        await store.release(key)
        log.warning("bid %s report post failed for %s: %s", kind, pk, exc)
        return "failed:post"
    return "posted"


async def run_bid_reports_tick(
    now: Optional[datetime] = None,
    *,
    store: Optional[ReportStore] = None,
    storage: Optional[BidStorage] = None,
    post=None,
    sched: Optional[BidSchedule] = None,
) -> dict[str, str]:
    now = now or datetime.now(timezone.utc)
    sched = sched or schedule_from_settings()
    store = store or BeanieReportStore()
    storage = storage or get_bid_storage()
    if post is None and bids_slack_configured():
        post = post_report
    result: dict[str, str] = {}
    for kind in KINDS:
        result[kind] = await _run_kind(kind, now, sched, store, storage, post)
        log.info("bid %s report: %s", kind, result[kind])
    return result
