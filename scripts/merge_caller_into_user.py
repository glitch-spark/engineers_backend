"""One-off: merge an external caller into a platform user (e.g. the caller "Nicholas" became a user "Nicholas").

Every round (Interview.stageHistory[].caller, and the legacy top-level Interview.caller) assigned to the external
caller is reassigned to the user: callerId = the user's id, callerKind = "user", callerName = the user's display name.
Then the external caller is archived (kept on the list for history, no longer offered). Only those caller subfields
are written, with positional ($set stageHistory.<i>.caller.*) updates; nothing else on an interview changes (confirmed included).

An interview's owner is never the caller of their own interview's rounds, so the user's own interviews are left
on the external caller and listed in the output.

--caller-name matches Caller.nameKey case-insensitively; --user-name matches UserExtra.name exactly, else
case-insensitively. If either matches 0 or more than 1 record the matches are printed and nothing changes (exit 1).
--caller-id / --user-id override the names.

Dry run by default: prints both people, the number of rounds and the companies. Idempotent.

Run:
    cd engineers_backend
    .venv/Scripts/python -m scripts.merge_caller_into_user --caller-name Nicholas --user-name Nicholas           # dry run
    .venv/Scripts/python -m scripts.merge_caller_into_user --caller-name Nicholas --user-name Nicholas --apply   # write

Connects with MONGODB_URI and registers the models only: no startup migrations or seeds, no index creation.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from beanie import PydanticObjectId

from app.caller_directory import user_display_name


@dataclass
class MergePlan:
    caller: object
    user: object
    rounds: int = 0
    companies: list[str] = field(default_factory=list)  # interviews that will change
    skipped_companies: list[str] = field(default_factory=list)  # the user's own interviews: left alone
    # (interview _id, indexes of its matching stageHistory rounds, whether its legacy top-level caller matches)
    targets: list = field(default_factory=list)


def _oid(value: str, what: str) -> PydanticObjectId:
    try:
        return PydanticObjectId(value)
    except Exception:
        raise ValueError(f"{what} {value!r} is not a valid id") from None


async def plan_merge(caller_id: str, user_id: str) -> MergePlan:
    from app.models import Caller, Interview, UserExtra

    caller = await Caller.get(_oid(caller_id, "caller id"))
    if caller is None:
        raise ValueError(f"no external caller with id {caller_id}")
    user = await UserExtra.get(_oid(user_id, "user id"))
    if user is None:
        raise ValueError(f"no user with id {user_id}")
    plan = MergePlan(caller=caller, user=user)
    cursor = Interview.get_motor_collection().find(
        {"$or": [{"stageHistory.caller.callerId": caller_id}, {"caller.callerId": caller_id}]},
        {"companyName": 1, "createdBy": 1, "caller.callerId": 1, "stageHistory.caller.callerId": 1},
    )
    async for doc in cursor:
        label = (doc.get("companyName") or "").strip() or f"(no company, {doc['_id']})"
        if str(doc.get("createdBy")) == str(user.id):
            plan.skipped_companies.append(label)
            continue
        idx = [i for i, r in enumerate(doc.get("stageHistory") or []) if (r.get("caller") or {}).get("callerId") == caller_id]
        top = (doc.get("caller") or {}).get("callerId") == caller_id
        # Interview.caller mirrors the latest round, so it is a round of its own only when no stageHistory round matches
        # (a legacy document that predates stageHistory); it is moved either way.
        plan.rounds += len(idx) or int(top)
        plan.companies.append(label)
        plan.targets.append((doc["_id"], idx, top))
    return plan


async def merge_caller_into_user(caller_id: str, user_id: str, *, apply: bool) -> int:
    """Rounds (stageHistory + top-level caller) assigned to the external caller; with apply, move them to the user."""
    plan = await plan_merge(caller_id, user_id)
    if not apply:
        return plan.rounds
    from app.models import Interview

    coll = Interview.get_motor_collection()
    uid, uname = str(plan.user.id), user_display_name(plan.user)
    # Positional paths (stageHistory.<i>.caller.x) guarded by the same indexes in the filter, so a round that moved or
    # changed since the scan is not touched. Only the three caller subfields are set.
    for iid, idx, top in plan.targets:
        sets: dict = {}
        guard: dict = {"_id": iid}
        for i in idx:
            guard[f"stageHistory.{i}.caller.callerId"] = caller_id
            sets.update({
                f"stageHistory.{i}.caller.callerId": uid,
                f"stageHistory.{i}.caller.callerKind": "user",
                f"stageHistory.{i}.caller.callerName": uname,
            })
        if top:
            guard["caller.callerId"] = caller_id
            sets.update({"caller.callerId": uid, "caller.callerKind": "user", "caller.callerName": uname})
        if sets:
            await coll.update_one(guard, {"$set": sets})
    if plan.caller.archivedAt is None:
        await type(plan.caller).get_motor_collection().update_one(
            {"_id": plan.caller.id, "archivedAt": None}, {"$set": {"archivedAt": datetime.now(timezone.utc)}}
        )
    return plan.rounds


async def _pick(label: str, matches: list, describe, explicit_id: Optional[str], model) -> Optional[object]:
    """The one record for this side, or None after printing why not."""
    if explicit_id:
        try:
            doc = await model.get(_oid(explicit_id, f"{label} id"))
        except ValueError as exc:
            print(f"ERROR: {exc}")
            return None
        if doc is None:
            print(f"ERROR: no {label} with id {explicit_id}")
        return doc
    if len(matches) == 1:
        return matches[0]
    print(f"ERROR: {len(matches)} {label} records match; need exactly 1." + ("" if matches else " (none)"))
    for m in matches:
        print("  ", describe(m))
    return None


def _describe_caller(c) -> str:
    return f"{c.name!r} id={c.id}" + (" (archived)" if c.archivedAt else "")


def _describe_user(u) -> str:
    return f"{user_display_name(u)!r} id={u.id} email={u.email}" + (" (inactive)" if u.isActive is False else "")


async def run(args: argparse.Namespace) -> int:
    from app.models import Caller, UserExtra

    if not (args.caller_name or args.caller_id) or not (args.user_name or args.user_id):
        print("ERROR: give --caller-name (or --caller-id) and --user-name (or --user-id)")
        return 2
    callers, users = [], []
    if args.caller_name and not args.caller_id:
        key = args.caller_name.strip().lower()
        callers = await Caller.find({"nameKey": key}).to_list()
    if args.user_name and not args.user_id:
        want = args.user_name.strip()
        everyone = await UserExtra.find_all().to_list()
        users = [u for u in everyone if (u.name or "").strip() == want]
        if len(users) != 1:  # exact wins only when unique
            users = [u for u in everyone if (u.name or "").strip().casefold() == want.casefold()]
    caller = await _pick("caller", callers, _describe_caller, args.caller_id, Caller)
    user = await _pick("user", users, _describe_user, args.user_id, UserExtra)
    if caller is None or user is None:
        return 1
    try:
        plan = await plan_merge(str(caller.id), str(user.id))
    except ValueError as exc:
        print(f"ERROR: {exc}")
        return 1
    print(f"Caller: {caller.name} ({caller.id})" + (" [already archived]" if caller.archivedAt else ""))
    print(f"User:   {user_display_name(user)} ({user.id})")
    print(f"Rounds to move: {plan.rounds} in {len(plan.companies)} interview(s)")
    for company in plan.companies:
        print(f"   - {company}")
    if plan.skipped_companies:
        print(f"Left on the external caller (the user owns these interviews): {', '.join(plan.skipped_companies)}")
    if args.apply:
        moved = await merge_caller_into_user(str(caller.id), str(user.id), apply=True)
        print(f"APPLIED: moved {moved} round(s) to {user_display_name(user)} and archived caller {caller.name}")
    else:
        print("DRY RUN: nothing written. Re-run with --apply to merge.")
    return 0


async def _connect():
    """Mongo + the models this script reads and writes. Deliberately not app.db.init_db (it writes migrations/seeds)."""
    from beanie import init_beanie
    from motor.motor_asyncio import AsyncIOMotorClient

    from app.config import get_settings
    from app.models import Account, Caller, Interview, InterviewQuestion, UserExtra

    client = AsyncIOMotorClient(get_settings().MONGODB_URI)
    await init_beanie(
        database=client.get_default_database(),
        document_models=[Interview, UserExtra, Account, InterviewQuestion, Caller],
        skip_indexes=True,
    )
    return client


async def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--caller-name", help="external caller's name (case-insensitive)")
    parser.add_argument("--user-name", help="platform user's name (exact, else case-insensitive)")
    parser.add_argument("--caller-id", help="external caller id (overrides --caller-name)")
    parser.add_argument("--user-id", help="user id (overrides --user-name)")
    parser.add_argument("--apply", action="store_true", help="write the merge (default: dry run)")
    args = parser.parse_args()
    client = await _connect()
    try:
        return await run(args)
    finally:
        client.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
