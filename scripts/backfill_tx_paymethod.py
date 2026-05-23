"""One-off backfill: set Transaction.payMethod + cardLast4 from the
matching CardLink (Subscription) for the same user.

Matching rule, per transaction:
  - Find CardLink rows where userId == tx.userId AND from_ <= tx.date <= to.
  - If one or more match, pick the most recently started (largest from_)
    and set payMethod='card', cardLast4=<last 4 of cardNumber>.
  - If no match, leave the transaction untouched (UI shows "—" until a
    user edits it).

Idempotent: rows that already have payMethod set are skipped.

Reads MONGODB_URI from the project's .env via app.config.get_settings.
The URI's path segment supplies the database name.

Run:
    cd engineers_backend
    .venv/bin/python -m scripts.backfill_tx_paymethod
"""

from __future__ import annotations

import asyncio
import sys

from motor.motor_asyncio import AsyncIOMotorClient

from app.config import get_settings


def _last4(card_number: str) -> str | None:
    digits = "".join(c for c in (card_number or "") if c.isdigit())
    if len(digits) < 4:
        return None
    return digits[-4:]


async def main() -> int:
    settings = get_settings()
    if not settings.MONGODB_URI:
        print("MONGODB_URI not set in .env", file=sys.stderr)
        return 1

    client = AsyncIOMotorClient(settings.MONGODB_URI)
    db = client.get_default_database()
    if db is None:
        print("MONGODB_URI must include a database name in its path", file=sys.stderr)
        return 1
    tx_coll = db["transactions"]
    cl_coll = db["cardlinks"]

    total = await tx_coll.count_documents({})
    needs = await tx_coll.count_documents({"payMethod": {"$in": [None, ""]}})
    print(f"transactions total={total} needing backfill={needs}")

    updated = 0
    unmatched = 0
    cursor = tx_coll.find({"payMethod": {"$in": [None, ""]}})
    async for tx in cursor:
        user_id = tx.get("userId")
        tx_date = tx.get("date")
        if user_id is None or tx_date is None:
            unmatched += 1
            continue

        # Most-recent active subscription covering tx_date.
        match = await cl_coll.find_one(
            {"userId": user_id, "from": {"$lte": tx_date}, "to": {"$gte": tx_date}},
            sort=[("from", -1)],
        )
        if match is None:
            unmatched += 1
            continue

        last4 = _last4(match.get("cardNumber") or "")
        if not last4:
            unmatched += 1
            continue

        await tx_coll.update_one(
            {"_id": tx["_id"]},
            {"$set": {"payMethod": "card", "cardLast4": last4}},
        )
        updated += 1

    print(f"updated={updated} unmatched={unmatched}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
