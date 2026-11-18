"""Per-profile data and the idle rule shared by the Bids and Profiles tabs."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional

from app import interview_rates
from app.stats import frame

IDLE_DAYS = 7


def idle_profiles(profile_last_bid: dict[str, Optional[date]], today: date, names: Optional[dict] = None) -> Optional[dict]:
    """`profile_last_bid` is keyed by profile id; `names` maps id -> display name (default: the id)."""
    names = names or {}
    ids = sorted(pid for pid, last in profile_last_bid.items() if last is None or (today - last).days >= IDLE_DAYS)
    ids.sort(key=lambda pid: (names.get(pid, pid), pid))
    idle = [names.get(pid, pid) for pid in ids]
    if not idle:
        return None
    shown = ", ".join(idle[:3]) + (f" and {len(idle) - 3} more" if len(idle) > 3 else "")
    return frame.tip(
        "bids.idle_profiles", "watch",
        f"{len(idle)} profile{'s' if len(idle) > 1 else ''} had no bids in the last {IDLE_DAYS} days: {shown}.",
        "Idle profiles lose search ranking on most job boards.",
        {"profileIds": ids, "profiles": idle}, len(idle) * 10)  # rough: ~10 bids a profile a day-ish


async def profile_last_bids(uid, region: str, today: date) -> tuple[dict[str, Optional[date]], dict[str, str]]:
    """Non-archived profiles of `uid` in the region → (profile id → last day, profile id → name); last day with a bid over the IDLE_DAYS ending `today`."""
    from app.models import DailyPlan
    from app.plan_profiles import owner_profiles

    profiles = [p for p in await owner_profiles(uid)
                if not p["archived"] and (region == "all" or p["region"] == region)]
    if not profiles:
        return {}, {}
    first = today - timedelta(days=IDLE_DAYS - 1)
    last: dict = {}
    for plan in await DailyPlan.find({"userId": uid, "date": {
            "$gte": datetime.combine(first, datetime.min.time()), "$lte": datetime.combine(today, datetime.min.time())}}).to_list():
        for pid, n in plan.done.bidsSelfByProfile.items():
            if n:
                last[pid] = max(last.get(pid, plan.date.date()), plan.date.date())
    by_profile: dict = {}
    await interview_rates.bidder_bids([uid], first, today, "all", {}, by_profile)
    for (pid, day), n in by_profile.items():
        if n:
            last[pid] = max(last.get(pid, day), day)
    return {p["id"]: last.get(p["id"]) for p in profiles}, {p["id"]: p["name"] for p in profiles}
