import logging
import os

from beanie import init_beanie
from motor.motor_asyncio import AsyncIOMotorClient

log = logging.getLogger(__name__)

_client = None


async def close_db() -> None:
    global _client
    if _client is not None:
        _client.close()
        _client = None

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
    Application,
    EmailAccount,
    EmailClassificationCache,
    EmailMessage,
    EmailRule,
    GlobalPrompt,
    Interview,
    InterviewChatLog,
    InterviewPrompt,
    InterviewQuestion,
    InterviewTemplateAnswer,
    Notification,
    RecurringCharge,
    ResumeApplication,
    ResumeLLMCache,
    Skill,
    Transaction,
    UserExtra,
    WeeklyPlan,
)
from app.models.slack_alert_log import SlackAlertLog

DOCUMENT_MODELS = [
    UserExtra,
    Transaction,
    Account,
    WeeklyPlan,
    Interview,
    InterviewChatLog,
    InterviewPrompt,
    InterviewQuestion,
    InterviewTemplateAnswer,
    Skill,
    AiReviewRun,
    AnalyzeChatLog,
    GlobalPrompt,
    ResumeApplication,
    Application,
    EmailAccount,
    EmailMessage,
    EmailRule,
    EmailClassificationCache,
    ResumeLLMCache,
    SlackAlertLog,
    Notification,
    RecurringCharge,
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


async def _migrate_default_leaderboard_optin(database) -> None:
    """Flip leaderboardOptIn=True for existing non-admin users that haven't
    explicitly opted out. Idempotent — only touches docs where the field is
    missing or False AND role != 'admin'. Admins keep their current value
    (default False) so workspace owners don't suddenly appear on rankings."""
    coll = database["userextras"]
    try:
        result = await coll.update_many(
            {
                "role": {"$ne": "admin"},
                "$or": [
                    {"leaderboardOptIn": {"$exists": False}},
                    {"leaderboardOptIn": False},
                ],
            },
            {"$set": {"leaderboardOptIn": True}},
        )
        if result.modified_count:
            log.info(
                "leaderboard opt-in default-on: flipped %d staff users",
                result.modified_count,
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("leaderboard opt-in migration failed: %s", exc)


async def _migrate_billing_cycle(database) -> None:
    """Existing ledger rows were treated as monthly. Fill the new field."""
    coll = database["transactions"]
    try:
        result = await coll.update_many(
            {"$or": [
                {"billingCycle": {"$exists": False}},
                {"billingCycle": None},
            ]},
            {"$set": {"billingCycle": "monthly"}},
        )
        if result.modified_count:
            log.info("billingCycle default monthly: updated %d transactions", result.modified_count)
    except Exception as exc:  # noqa: BLE001
        log.warning("billingCycle migration failed: %s", exc)


async def _migrate_legacy_payer_to_admin(database) -> None:
    """Pre-payer ledger rows through today were paid by admin. Stamp payerId
    on anything missing it with date <= end of today. Idempotent."""
    from datetime import datetime, timezone

    users = database["userextras"]
    txs = database["transactions"]
    try:
        admin = await users.find_one({"role": "admin"}, sort=[("createdAt", 1)])
        if admin is None:
            log.warning("legacy payer backfill skipped: no admin user")
            return
        now = datetime.now(timezone.utc)
        cutoff = datetime(now.year, now.month, now.day, 23, 59, 59, 999000)
        result = await txs.update_many(
            {
                "$and": [
                    {"$or": [
                        {"payerId": {"$exists": False}},
                        {"payerId": None},
                    ]},
                    {"date": {"$lte": cutoff}},
                ]
            },
            {"$set": {"payerId": admin["_id"]}},
        )
        if result.modified_count:
            log.info(
                "legacy payer -> admin %s: updated %d transactions",
                admin.get("email") or admin["_id"],
                result.modified_count,
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("legacy payer backfill failed: %s", exc)


async def init_db():
    global _client
    uri = os.environ["MONGODB_URI"]
    client = AsyncIOMotorClient(uri)
    _client = client
    database = client.get_default_database()
    await _drop_stale_indexes(database)
    await _migrate_legacy_resume_apps(database)
    await _migrate_default_leaderboard_optin(database)
    await _migrate_billing_cycle(database)
    await _migrate_legacy_payer_to_admin(database)
    await init_beanie(database=database, document_models=DOCUMENT_MODELS)
    try:
        from app.alerts_service import seed_recurring_charges_from_transactions

        seeded = await seed_recurring_charges_from_transactions()
        if seeded:
            log.info("seeded %d recurring charges from transactions", seeded)
    except Exception as exc:  # noqa: BLE001
        log.warning("recurring charge seed failed: %s", exc)
    return client
