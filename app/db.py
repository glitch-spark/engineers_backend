import logging
import os

from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorClient

log = logging.getLogger(__name__)

# Index names left behind by older schema versions. Drop on startup so
# unique constraints from the old (profileId, sync-only) shape don't reject
# inserts under the new schema (accountId-based, multiple jobs per company).
STALE_INDEXES: dict[str, list[str]] = {
    "resumeapplications": [
        "uniq_user_profile_company",
        "uniq_user_account_company",
    ],
}

from app.models import (
    Account,
    AiReviewRun,
    AnalyzeChatLog,
    CardLink,
    GlobalPrompt,
    Interview,
    InterviewChatLog,
    InterviewQuestion,
    ResumeApplication,
    Skill,
    Transaction,
    UserExtra,
    WeeklyPlan,
)

DOCUMENT_MODELS = [
    UserExtra,
    Transaction,
    Account,
    CardLink,
    WeeklyPlan,
    Interview,
    InterviewChatLog,
    InterviewQuestion,
    Skill,
    AiReviewRun,
    AnalyzeChatLog,
    GlobalPrompt,
    ResumeApplication,
]


async def _migrate_legacy_resume_apps(database) -> None:
    """Old schema used `profileId`; new uses `accountId`. Rename in-place so
    Beanie validation doesn't reject historical rows."""
    coll = database["resumeapplications"]
    try:
        result = await coll.update_many(
            {"profileId": {"$exists": True}, "accountId": {"$exists": False}},
            {"$rename": {"profileId": "accountId"}},
        )
        if result.modified_count:
            log.info(
                "renamed profileId -> accountId on %d resumeapplications rows",
                result.modified_count,
            )
        # Drop any remaining `profileId` field on rows that had both.
        await coll.update_many(
            {"profileId": {"$exists": True}},
            {"$unset": {"profileId": ""}},
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("legacy resumeapplications migration failed: %s", exc)


async def _drop_stale_indexes(database) -> None:
    for collection_name, names in STALE_INDEXES.items():
        coll = database[collection_name]
        try:
            existing = await coll.index_information()
        except Exception as exc:  # noqa: BLE001
            log.warning("could not list indexes on %s: %s", collection_name, exc)
            continue
        for name in names:
            if name in existing:
                try:
                    await coll.drop_index(name)
                    log.info("dropped stale index %s on %s", name, collection_name)
                except Exception as exc:  # noqa: BLE001
                    log.warning("failed to drop %s on %s: %s", name, collection_name, exc)


async def _migrate_style_markdown_tokens(database) -> None:
    """Existing accounts predate the tagged-markdown bridge: their
    `styleConfig` was authoritative but `styleMarkdown` either empty or
    pure prose with no @style tokens. Regenerate `styleMarkdown` so the
    Markdown-mode editor and the backend prompt builder see the same data.
    Idempotent — accounts that already have tokens are skipped."""
    from app.style_tokens import (
        extract_notes,
        parse_tagged_markdown,
        serialize_to_tagged_markdown,
    )

    coll = database["accounts"]
    try:
        cursor = coll.find({"styleConfig": {"$exists": True, "$ne": None}})
        migrated = 0
        async for doc in cursor:
            cfg = doc.get("styleConfig")
            if not isinstance(cfg, dict) or not cfg:
                continue
            md = (doc.get("styleMarkdown") or doc.get("styles") or "").strip()
            _, _, token_count = parse_tagged_markdown(md)
            if token_count > 0:
                continue
            # Preserve hand-written prose: if user has markdown but no notes
            # heading, wrap the whole thing as user notes so it survives.
            seed = md
            if md and not extract_notes(md):
                seed = f"## User notes\n\n{md}"
            new_md = serialize_to_tagged_markdown(cfg, seed)
            await coll.update_one({"_id": doc["_id"]}, {"$set": {"styleMarkdown": new_md}})
            migrated += 1
        if migrated:
            log.info("backfilled @style tokens on %d account(s)", migrated)
    except Exception as exc:  # noqa: BLE001
        log.warning("style-markdown token migration failed: %s", exc)


async def init_db() -> None:
    uri = os.environ["MONGODB_URI"]
    client = AsyncIOMotorClient(uri)
    database = client.get_default_database()
    await _drop_stale_indexes(database)
    await _migrate_legacy_resume_apps(database)
    # Style-markdown backfill intentionally not invoked — defer until needed.
    # Existing accounts get tokens lazily on next save via _sync_style_fields.
    await init_beanie(database=database, document_models=DOCUMENT_MODELS)
