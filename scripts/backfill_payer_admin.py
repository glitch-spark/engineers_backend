"""Stamp payerId=admin on transactions through today that have no payer.

Idempotent. Also runs automatically on app startup (app.db).

    python -m scripts.backfill_payer_admin
"""
from __future__ import annotations

import asyncio

from motor.motor_asyncio import AsyncIOMotorClient

from app.config import get_settings
from app.db import _migrate_legacy_payer_to_admin


async def main() -> int:
    settings = get_settings()
    client = AsyncIOMotorClient(settings.MONGODB_URI)
    database = client.get_default_database()
    users = database["userextras"]
    txs = database["transactions"]
    admin = await users.find_one({"role": "admin"}, sort=[("createdAt", 1)])
    before = await txs.count_documents({
        "$or": [{"payerId": {"$exists": False}}, {"payerId": None}],
    })
    await _migrate_legacy_payer_to_admin(database)
    after = await txs.count_documents({
        "$or": [{"payerId": {"$exists": False}}, {"payerId": None}],
    })
    print(f"admin={admin.get('email') if admin else None}")
    print(f"missing_payer_before={before}")
    print(f"missing_payer_after={after}")
    client.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
