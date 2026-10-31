"""Link caller rounds that only have a typed caller name to the caller list.

Rounds saved before the caller list have caller.callerName but no callerId, so Statistics → Callers counts them as
Unassigned. Run once:
  python -m scripts.link_typed_callers          # dry run: prints each change and the names left as they are
  python -m scripts.link_typed_callers --apply

A typed name matches a caller's name ignoring case and spacing, or an alias below. TBD, Anyone and unknown names are
left as they are. Idempotent: linked rounds have a callerId and are skipped.
"""

from __future__ import annotations

import asyncio
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from motor.motor_asyncio import AsyncIOMotorClient  # noqa: E402

from app.config import get_settings  # noqa: E402

# Other spellings typed for a caller: typed key → the caller's nameKey.
ALIASES = {"mohammad": "muhammad", "mohammed": "muhammad", "muhammed": "muhammad"}


def name_key(name: str) -> str:
    return " ".join((name or "").split()).lower()


def plan_links(doc: dict, callers: dict[str, dict]) -> list[tuple[int, dict, dict]]:
    """For one raw interview: (round index, round, caller) for each caller round with a typed name that matches a
    caller (`callers` is nameKey → caller document)."""
    out = []
    for i, e in enumerate(doc.get("stageHistory") or []):
        c = e.get("caller") or {}
        if not c.get("enabled") or c.get("callerId"):
            continue
        key = name_key(c.get("callerName") or "")
        caller = callers.get(ALIASES.get(key, key))
        if caller is not None:
            out.append((i, e, caller))
    return out


async def run(apply: bool) -> int:
    db = AsyncIOMotorClient(get_settings().MONGODB_URI).get_default_database()
    callers = {c["nameKey"]: c async for c in db.callers.find()}
    linked, left = 0, Counter()
    async for doc in db.interviews.find({"stageHistory.caller.enabled": True}):
        links = plan_links(doc, callers)
        for i, e, caller in links:
            linked += 1
            when = e.get("scheduledAt") or e.get("at")
            print(f"  {doc['_id']} round {e.get('id') or i} ({doc.get('companyName') or '?'}, {when:%Y-%m-%d}): "
                  f"{(e.get('caller') or {}).get('callerName')!r} -> {caller['name']}")
            if apply:
                payload = {"callerId": str(caller["_id"]), "callerName": caller["name"]}
                if e.get("id"):
                    await db.interviews.update_one(
                        {"_id": doc["_id"], "stageHistory.id": e["id"]},
                        {"$set": {f"stageHistory.$.caller.{k}": v for k, v in payload.items()}},
                    )
                else:  # a round saved before rounds had ids
                    await db.interviews.update_one(
                        {"_id": doc["_id"]}, {"$set": {f"stageHistory.{i}.caller.{k}": v for k, v in payload.items()}},
                    )
        # The interview-level caller mirrors the latest round's.
        top = doc.get("caller") or {}
        tip = (doc.get("stageHistory") or [None])[-1]
        if apply and top.get("enabled") and not top.get("callerId") and any(e is tip for _, e, _ in links):
            caller = next(c for _, e, c in links if e is tip)
            await db.interviews.update_one(
                {"_id": doc["_id"]}, {"$set": {"caller.callerId": str(caller["_id"]), "caller.callerName": caller["name"]}},
            )
        matched = {id(e) for _, e, _ in links}
        for e in doc.get("stageHistory") or []:
            c = e.get("caller") or {}
            if c.get("enabled") and not c.get("callerId") and id(e) not in matched:
                left[c.get("callerName") or "(blank)"] += 1

    print(f"mode={'applied' if apply else 'dry-run'} rounds linked={linked}")
    print(f"  left unassigned: {dict(left)}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(run(apply="--apply" in sys.argv)))
