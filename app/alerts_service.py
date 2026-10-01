"""Create / upsert in-app notifications. No Slack."""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone
from typing import Any, Optional

from beanie import PydanticObjectId
from pymongo.errors import DuplicateKeyError

from app.alerts import (
    LEAD_DAYS,
    card_renewal_href,
    charge_title,
    daily_bids_target,
    days_until,
    due_phrase,
    format_amount,
    in_reminder_window,
    app_week_key,
    local_day_bounds_utc,
    local_now,
    monday_morning_window,
    next_due_date,
    paid_for_period,
    period_key_for_due,
)
from app.models import Notification, RecurringCharge, ResumeApplication, Transaction, UserExtra, WeeklyPlan
from app.models.plan_common import monday_of
from app.plan_auto import weekly_plans_for
from app.slack_bot import resolve_user_tz

log = logging.getLogger(__name__)


async def upsert_charge_from_transaction(tx: Transaction) -> Optional[RecurringCharge]:
    if tx.payMethod != "card" or not (tx.cardLast4 or "").strip():
        return None
    if (tx.billingCycle or "monthly") != "monthly":
        return None
    last4 = tx.cardLast4.strip()
    title = charge_title(tx.description, last4)
    label = (tx.cardLabel or "").strip() or None
    billing_day = tx.date.day if tx.date else 1

    existing = await RecurringCharge.find_one(
        {"userId": tx.userId, "cardLast4": last4, "title": title}
    )
    if existing is None:
        charge = RecurringCharge(
            userId=tx.userId,
            title=title,
            cardLast4=last4,
            cardLabel=label,
            amount=float(tx.amount or 0),
            billingDay=billing_day,
            status="active",
            lastPaidOn=tx.date,
            sourceTransactionId=tx.id,
        )
        try:
            await charge.insert()
        except DuplicateKeyError:
            existing = await RecurringCharge.find_one(
                {"userId": tx.userId, "cardLast4": last4, "title": title}
            )
            if existing is None:
                raise
            charge = existing
        else:
            return charge

    existing.status = "active"
    existing.cardLabel = label or existing.cardLabel
    existing.amount = float(tx.amount or 0)
    existing.billingDay = billing_day
    existing.lastPaidOn = tx.date
    existing.sourceTransactionId = tx.id
    await existing.save()
    return existing


async def _admins() -> list[UserExtra]:
    return await UserExtra.find({"role": "admin", "isActive": {"$ne": False}}).to_list()


async def _put_notification(
    *,
    user_id: PydanticObjectId,
    kind: str,
    title: str,
    body: str,
    href: str,
    period_key: str,
    dedupe_id: str,
    meta: dict[str, Any],
    owner_user_id: Optional[PydanticObjectId] = None,
) -> bool:
    existing = await Notification.find_one(
        {
            "userId": user_id,
            "kind": kind,
            "periodKey": period_key,
            "dedupeId": dedupe_id,
        }
    )
    if existing is not None:
        return False
    doc = Notification(
        userId=user_id,
        kind=kind,  # type: ignore[arg-type]
        title=title,
        body=body,
        href=href,
        unread=True,
        periodKey=period_key,
        dedupeId=dedupe_id,
        meta=meta,
        ownerUserId=owner_user_id,
    )
    try:
        await doc.insert()
    except DuplicateKeyError:
        return False
    return True


def _last_paid_date(charge: RecurringCharge) -> Optional[date]:
    if charge.lastPaidOn is None:
        return None
    return charge.lastPaidOn.date() if hasattr(charge.lastPaidOn, "date") else charge.lastPaidOn


async def _tick_card_renewals(*, today_by_user: dict[str, date]) -> int:
    created = 0
    charges = await RecurringCharge.find({"status": "active"}).to_list()
    if not charges:
        return 0
    owner_ids = list({c.userId for c in charges})
    owners = await UserExtra.find({"_id": {"$in": owner_ids}}).to_list()
    owners_by_id = {str(u.id): u for u in owners}
    admins = await _admins()

    for charge in charges:
        owner = owners_by_id.get(str(charge.userId))
        if owner is None:
            continue
        today = today_by_user.get(str(owner.id))
        if today is None:
            today = local_now(resolve_user_tz(owner)).date()
        if not in_reminder_window(today, charge.billingDay, LEAD_DAYS):
            continue
        due = next_due_date(today, charge.billingDay)
        period = period_key_for_due(due)
        if charge.ackedPeriod == period:
            continue
        if paid_for_period(_last_paid_date(charge), due):
            continue

        days = days_until(due, today)
        when = due_phrase(days)
        amount = format_amount(charge.amount)
        href = card_renewal_href(
            last4=charge.cardLast4,
            title=charge.title,
            amount=charge.amount,
            card_label=charge.cardLabel,
        )
        owner_name = owner.name or owner.email or "Teammate"
        body = (
            f"{charge.title} on ****{charge.cardLast4} is due {when} ({amount}). "
            "Log this month's charge?"
        )
        meta = {
            "chargeId": str(charge.id),
            "cardLast4": charge.cardLast4,
            "cardLabel": charge.cardLabel,
            "amount": charge.amount,
            "billingDate": due.isoformat(),
            "ownerUserId": str(owner.id),
            "ownerName": owner_name,
        }
        owner_title = f"{charge.title} · ****{charge.cardLast4}"
        if await _put_notification(
            user_id=owner.id,
            kind="card_renewal",
            title=owner_title,
            body=body,
            href=href,
            period_key=period,
            dedupe_id=str(charge.id),
            meta=meta,
            owner_user_id=owner.id,
        ):
            created += 1

        admin_title = f"{owner_name} · {charge.title} · ****{charge.cardLast4}"
        for admin in admins:
            if str(admin.id) == str(owner.id):
                continue
            if await _put_notification(
                user_id=admin.id,
                kind="card_renewal",
                title=admin_title,
                body=body,
                href=href,
                period_key=period,
                dedupe_id=str(charge.id),
                meta=meta,
                owner_user_id=owner.id,
            ):
                created += 1
    return created


async def _tick_weekly_plans(users: list[UserExtra], *, now: datetime) -> int:
    created = 0
    for user in users:
        local = local_now(resolve_user_tz(user), now)
        if not monday_morning_window(local):
            continue
        year, week, period = app_week_key(local)
        existing_plan = await WeeklyPlan.find_one(
            {"userId": user.id, "year": year, "weekNumber": week}
        )
        if existing_plan is not None:
            continue
        if await _put_notification(
            user_id=user.id,
            kind="weekly_plan",
            title="New week — add this week's plan",
            body="It's Monday. Set targets before the week gets away from you.",
            href="/weekly-plan",
            period_key=period,
            dedupe_id="weekly_plan",
            meta={"year": year, "weekNumber": week},
            owner_user_id=user.id,
        ):
            created += 1
    return created


async def _tick_daily_bids(users: list[UserExtra], *, now: datetime) -> int:
    created = 0
    for user in users:
        local = local_now(resolve_user_tz(user), now)
        plan = (await weekly_plans_for([user.id], monday_of(local.date()))).get(user.id)
        if plan is None:
            continue
        target = daily_bids_target(plan.goal.bidsSelf + plan.goal.bidsBidder)
        if target <= 0:
            continue
        start, end = local_day_bounds_utc(local)
        # ResumeApplication.createdAt is naive UTC in this codebase.
        start_naive = start.replace(tzinfo=None)
        end_naive = end.replace(tzinfo=None)
        count = await ResumeApplication.find(
            {"userId": user.id, "createdAt": {"$gte": start_naive, "$lt": end_naive}}
        ).count()
        if count < target:
            continue
        period = local.date().isoformat()
        if await _put_notification(
            user_id=user.id,
            kind="daily_bids",
            title=f"Daily apply pace hit ({count}/{target})",
            body="You've reached today's implied apply target from the weekly plan.",
            href="/resume",
            period_key=period,
            dedupe_id="daily_bids",
            meta={"count": count, "target": target},
            owner_user_id=user.id,
        ):
            created += 1
    return created


async def run_alerts_tick(*, now: Optional[datetime] = None) -> dict[str, int]:
    clock = now or datetime.now(timezone.utc)
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=timezone.utc)

    users = await UserExtra.find({"isActive": {"$ne": False}}).to_list()
    today_by_user = {
        str(u.id): local_now(resolve_user_tz(u), clock).date() for u in users
    }
    card_n = await _tick_card_renewals(today_by_user=today_by_user)
    week_n = await _tick_weekly_plans(users, now=clock)
    bids_n = await _tick_daily_bids(users, now=clock)
    return {
        "cardRenewal": card_n,
        "weeklyPlan": week_n,
        "dailyBids": bids_n,
        "created": card_n + week_n + bids_n,
    }


async def seed_recurring_charges_from_transactions() -> int:
    """Idempotent backfill: latest monthly card tx per (user, last4, title)."""
    pipeline = [
        {"$match": {
            "payMethod": "card",
            "cardLast4": {"$nin": [None, ""]},
            "$or": [
                {"billingCycle": "monthly"},
                {"billingCycle": {"$exists": False}},
                {"billingCycle": None},
            ],
        }},
        {"$sort": {"date": -1, "createdAt": -1}},
        {"$group": {
            "_id": {
                "userId": "$userId",
                "cardLast4": "$cardLast4",
                "title": {"$ifNull": ["$description", ""]},
            },
            "doc": {"$first": "$$ROOT"},
        }},
    ]
    rows = await Transaction.aggregate(pipeline).to_list()
    seeded = 0
    for row in rows:
        raw = row.get("doc") or {}
        tx = await Transaction.get(raw.get("_id"))
        if tx is None:
            continue
        before = await RecurringCharge.find_one(
            {
                "userId": tx.userId,
                "cardLast4": tx.cardLast4,
                "title": charge_title(tx.description, tx.cardLast4 or ""),
            }
        )
        await upsert_charge_from_transaction(tx)
        if before is None:
            seeded += 1
    return seeded
