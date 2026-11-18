"""Statistics → Earnings: the subject's net (approved transactions) against the team and their own 3-period average."""
from __future__ import annotations

from calendar import monthrange
from collections import defaultdict
from datetime import date, datetime, timedelta
from statistics import mean
from typing import Optional

from app import interview_rates
from app.stats import frame

BELOW_RATIO = 0.8
ABOVE_RATIO = 1.2
DEFINITIONS = [
    "Earnings count approved transactions only, by the transaction's date.",
    "Net is income minus expenses; the team is everyone with an approved transaction in the period.",
]


def _usd(v: float) -> str:
    if v and abs(v) < 0.005:
        return "<$0.01"
    return f"{'-' if v < 0 else ''}${abs(v):,.0f}" if abs(v) >= 10 else f"{'-' if v < 0 else ''}${abs(v):.2f}"


def _tip(id: str, severity: str, text: str, detail: str, evidence: dict, impact: float) -> dict:
    return frame.tip(f"earnings.{id}", severity, text, detail, evidence, impact)


def negative_period(net: float) -> Optional[dict]:
    if net >= 0:
        return None
    return _tip("negative_period", "bad", f"You spent more than you earned this period ({_usd(net)}).",
                "Expenses exceeded income on approved transactions.", {"net": net}, -net)


def below_own_average(net: float, own_avg: Optional[float]) -> Optional[dict]:
    if not own_avg or own_avg <= 0 or net >= BELOW_RATIO * own_avg:
        return None
    gap = 1 - net / own_avg
    return _tip("below_own_average", "watch", f"Net is {gap:.0%} below your average of the last 3 periods ({_usd(own_avg)}).",
                f"{_usd(net)} this period vs {_usd(own_avg)} on average.", {"net": net, "ownAvg": own_avg}, own_avg - net)


def above_own_average(net: float, own_avg: Optional[float]) -> Optional[dict]:
    if not own_avg or own_avg <= 0 or net < ABOVE_RATIO * own_avg:
        return None
    gain = net / own_avg - 1
    return _tip("above_own_average", "good", f"Net is {gain:.0%} above your average of the last 3 periods ({_usd(own_avg)}) — keep it.",
                f"{_usd(net)} this period vs {_usd(own_avg)} on average.", {"net": net, "ownAvg": own_avg}, net - own_avg)


def no_earnings(has_transactions: bool) -> Optional[dict]:
    if has_transactions:
        return None
    return _tip("no_earnings", "watch", "No earnings recorded this period.",
                "No approved transactions with a date in this period.", {}, 0)


def _previous_windows(period: str, start: date, end: date) -> list[tuple[date, date]]:
    """The 3 full periods of the same type immediately before, nearest first."""
    out, s = [], start
    for _ in range(3):
        if period == "month":
            ps = date(s.year - (s.month == 1), s.month - 1 or 12, 1)
            pe = ps.replace(day=monthrange(ps.year, ps.month)[1])
        elif period == "week":
            ps, pe = s - timedelta(days=7), s - timedelta(days=1)
        else:
            n = (end - start).days + 1
            ps, pe = s - timedelta(days=n), s - timedelta(days=1)
        out.append((ps, pe))
        s = ps
    return out


def _mid(d: date) -> datetime:
    return datetime.combine(d, datetime.min.time())


async def earnings_view(subject, period: str, day: date, region: str, until: Optional[date], today: date) -> dict:
    from app.models import Transaction
    from app.routers.dashboard import _active_members
    from app.routers.metrics import _display_name

    start, end = interview_rates.window(period, day, until)
    prev = _previous_windows(period, start, end)
    members = {u.id: u for u in await _active_members()}
    ids = list(members) + ([] if subject.id in members else [subject.id])
    txs = await Transaction.find({
        "userId": {"$in": ids}, "status": "approved",
        "date": {"$gte": _mid(prev[-1][0]), "$lt": _mid(end + timedelta(days=1))}}).to_list()

    days: dict = defaultdict(lambda: (0.0,))  # (day, uid) → (net,): a tuple because frame.trend sums values[parts]
    tx_count: dict = defaultdict(int)
    income: dict = defaultdict(float)
    outcome: dict = defaultdict(float)
    net: dict = defaultdict(float)
    own_prev = [0.0] * 3
    first = await Transaction.find({"userId": subject.id, "status": "approved"}).sort("date").limit(1).to_list()
    since = first[0].date.date() if first else None
    k = sum(1 for _, pe in prev if since and pe >= since)  # only periods on/after the first transaction count
    for t in txs:
        d = t.date.date()
        if d < start:
            if t.userId == subject.id:
                own_prev[next(i for i, (a, b) in enumerate(prev) if a <= d <= b)] += t.amount
            continue
        days[(d, t.userId)] = (days[(d, t.userId)][0] + t.amount,)
        tx_count[t.userId] += 1
        net[t.userId] += t.amount
        (income if t.amount > 0 else outcome)[t.userId] += abs(t.amount)

    team_ids = [u for u in members if tx_count[u]]
    in_team = subject.id in team_ids
    nets = [round(net[u], 2) for u in team_ids]
    me = round(net[subject.id], 2)
    dist = frame.distribution(nets)
    rank, of = frame.rank_of(me, nets, True, in_team)
    n_days = len(frame.working_days(start, end, today))
    own_avg = round(sum(own_prev) / k, 2) if k else None
    avg_note = ("no earlier periods" if own_avg is None else
                f"this period {((me - own_avg) / abs(own_avg)):+.0%} vs average".replace("-", "\u2212")
                if own_avg else "average is $0")
    headline = [
        {"key": "net", "label": "Net this period", "value": me, "format": "usd", "team": dist, "rank": rank, "of": of,
         "vsMedian": frame.vs_median(me, dist["median"]), "higherIsBetter": True,
         "note": f"income {_usd(income[subject.id])} · expenses {_usd(outcome[subject.id])}"},
        {"key": "rank", "label": "Your rank", "value": rank, "format": "rank", "team": None, "rank": rank, "of": of,
         "vsMedian": None, "higherIsBetter": False, "note": f"of {of}"},
        {"key": "ownAvg", "label": "Your 3-period average", "value": own_avg, "format": "usd", "team": None,
         "rank": None, "of": None, "vsMedian": None, "higherIsBetter": True, "note": avg_note},
    ]

    rows = []
    for uid in team_ids + ([] if in_team else [subject.id]):
        r, _ = frame.rank_of(round(net[uid], 2), nets, True, uid in team_ids)
        rows.append({"userId": str(uid), "name": _display_name(members.get(uid, subject)), "value": round(net[uid], 2),
                     "vsMedian": frame.vs_median(net[uid], dist["median"]), "rank": r,
                     "parts": {"income": round(income[uid], 2), "outcome": round(outcome[uid], 2)},
                     "isSubject": uid == subject.id})
    rows.sort(key=lambda r: (-r["value"], r["name"].lower()))

    trend = frame.trend(start, end, today, days, (0,), subject.id, team_ids)
    for b in trend["buckets"]:
        b["you"], b["median"] = round(b["you"], 2), round(b["median"], 2)

    not_enough = frame.not_enough_data(len(team_ids), n_days)
    tips: list[dict] = []
    if not not_enough:
        active = bool(tx_count[subject.id])  # no transactions this period: "100% below" would only repeat no_earnings
        tips = frame.order_tips([t for t in (
            negative_period(me), active and below_own_average(me, own_avg), active and above_own_average(me, own_avg),
            no_earnings(bool(tx_count[subject.id]))) if t])

    return {
        "tab": "earnings", "label": interview_rates.label(period, start, end),
        "subject": {"userId": str(subject.id), "name": _display_name(subject)},
        "headline": headline, "tips": tips, "everyone": {"rowKind": "person", "metric": "net", "rows": rows},
        "trend": trend, "notEnoughData": not_enough, "definitions": DEFINITIONS,
    }
