"""Bidders page banners (spec 2026-11-14-bidders-insights-design.md): one owner's bidders that need action (red, one
per bidder, the first kind that applies) and archive suggestions (grey). Fixed windows ending today, whatever the
Performance period. Bidders that never registered are left out of both (their bids may come from folders, which
these counts don't see), except an expired, unused invite, which is an archive suggestion."""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone

from app.bids.performance import _local_day, day_counts, interview_credit, last_bid_at
from app.bids.windows import schedule_from_settings
from app.models import Account
from app.models.bid import Bid
from app.models.bidder import Bidder
from app.profile_metrics import bid_report_day

DAYS = 14             # the window flags look at
QUIET_WEEKDAYS = 3    # inactive: no bids on this many weekdays before today
MIN_REVIEWED = 20     # rejects: at least this many reviewed ...
REJECT_SHARE = 0.2    # ... and at least this share rejected
MIN_BIDS = 100        # no_interviews: at least this many bids with no interview
BACKLOG_DAYS = 2      # review_backlog: pending bids whose report day is at least this old


def _weekdays_before(today: date, n: int) -> list[date]:
    out, d = [], today
    while len(out) < n:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            out.append(d)
    return out


async def bid_alerts(bidders: list[Bidder], today: date, now: datetime) -> dict:
    """{asOf, flags: [{bidderId, name, kind, detail}], archive: [{bidderId, name, reason, since}]} for these bidders
    (archived ones skipped). Flag kinds, first that applies: inactive, rejects, no_interviews, review_backlog."""
    sched = schedule_from_settings()
    active = [b for b in bidders if b.archivedAt is None]
    out: dict = {"asOf": today.isoformat(), "flags": [], "archive": []}
    if not active:
        return out
    first = today - timedelta(days=DAYS - 1)
    ids = [b.id for b in active]
    counts = await day_counts(active, first, today, sched)
    per: dict = defaultdict(lambda: [0, 0, 0])
    for (bid, _d), c in counts.items():
        for i in range(3):
            per[bid][i] += c[i]
    quiet = _weekdays_before(today, QUIET_WEEKDAYS)
    profiles = list({b.profileId for b in active if b.profileId})
    tagged, untagged = await interview_credit(profiles, ids, first, today)
    names = {a.id: a.name for a in await Account.find({"_id": {"$in": profiles}}).to_list()} if profiles else {}
    last = await last_bid_at(ids)

    # Rejected bids' reasons over the window, and every pending bid's report day (the owner's backlog, any age).
    reasons: dict = defaultdict(Counter)
    since = datetime.combine(first - timedelta(days=1), datetime.min.time())
    async for d in Bid.get_motor_collection().find(
        {"bidderId": {"$in": ids}, "status": "rejected", "firstAt": {"$gte": since}},
        {"bidderId": 1, "firstAt": 1, "rejectReason": 1},
    ):
        if first <= bid_report_day(d["firstAt"], sched) <= today and d.get("rejectReason"):
            reasons[str(d["bidderId"])][d["rejectReason"]] += 1
    backlog: dict = defaultdict(list)
    async for d in Bid.get_motor_collection().find(
        {"bidderId": {"$in": ids}, "status": "pending"}, {"bidderId": 1, "firstAt": 1},
    ):
        day = bid_report_day(d["firstAt"], sched)
        if day <= today - timedelta(days=BACKLOG_DAYS):
            backlog[str(d["bidderId"])].append(day)

    naive_now = now.astimezone(timezone.utc).replace(tzinfo=None) if now.tzinfo else now
    for b in sorted(active, key=lambda x: x.name.lower()):
        k = str(b.id)
        approved, pending, rejected = per[k]
        last_day = _local_day(last[k], sched).isoformat() if k in last else None
        old_enough = _local_day(b.createdAt, sched) <= first
        if b.registeredAt is None:
            if b.inviteExpiresAt and b.inviteExpiresAt <= naive_now:
                out["archive"].append({"bidderId": k, "name": b.name, "reason": "invite_expired",
                                       "since": _local_day(b.inviteExpiresAt, sched).isoformat()})
            continue
        if old_enough and approved + pending + rejected == 0:
            out["archive"].append({"bidderId": k, "name": b.name, "reason": "idle", "since": last_day})
            continue
        flag = None
        if old_enough and all(sum(counts.get((k, d), (0, 0, 0))) == 0 for d in quiet):
            flag = ("inactive", {"lastBidDay": last_day})
        elif old_enough and approved + rejected >= MIN_REVIEWED and rejected / (approved + rejected) >= REJECT_SHARE:
            top = reasons[k].most_common(1)
            flag = ("rejects", {"rejected": rejected, "reviewed": approved + rejected,
                                "topReason": top[0][0] if top else None})
        elif old_enough and b.profileId and approved + pending >= MIN_BIDS \
                and tagged.get(k, 0) + untagged.get(str(b.profileId), 0) == 0:
            flag = ("no_interviews", {"bids": approved + pending, "profileId": str(b.profileId),
                                      "profileName": names.get(b.profileId, "")})
        elif backlog[k]:
            flag = ("review_backlog", {"pending": len(backlog[k]), "oldestDay": min(backlog[k]).isoformat()})
        if flag:
            out["flags"].append({"bidderId": k, "name": b.name, "kind": flag[0], "detail": flag[1]})
    return out
