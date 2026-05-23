"""One-off rename: Transaction.payMethod 'crypto' -> 'coin'.

Run once after deploying the model change. Idempotent.

Reads MONGODB_URI from .env via app.config.get_settings; DB name from URI path.

Run:
    cd engineers_backend
    .venv/bin/python -m scripts.rename_paymethod_crypto_to_coin
"""

from __future__ import annotations

import asyncio
import sys

from motor.motor_asyncio import AsyncIOMotorClient

from app.config import get_settings


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
    coll = db["transactions"]
    res = await coll.update_many({"payMethod": "crypto"}, {"$set": {"payMethod": "coin"}})
    print(f"renamed {res.modified_count} rows (matched {res.matched_count})")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
