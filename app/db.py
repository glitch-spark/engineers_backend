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
    # 7-day TTL replaced by "ttl_30d" (a TTL's duration can't change in place).
    "job_posting_cache": ["ttl"],
    # Unique key moved from resumeId to fileKey (uploaded or tailored file).
    "job_application_log": ["user_url_resume"],
    # Sparse unique index on seedKey allowed only one user-added eval JD (explicit null); replaced by "seed_key_str".
    "eval_job_descriptions": ["seed_key"],
}

from app.models import (
    Account,
    AiReviewRun,
    AiUsageEvent,
    AnalyzeChatLog,
    Bid,
    BidReport,
    Bidder,
    Caller,
    GlobalPrompt,
    Interview,
    InterviewChatLog,
    InterviewQuestion,
    JobApplicationLog,
    JobApplyRow,
    JobApplyRun,
    JobExtractionCache,
    JobInfo,
    JobApproval,
    JobInvalidRole,
    JobPostingCache,
    Notification,
    RecurringCharge,
    ResumeApplication,
    ResumeLLMCache,
    Skill,
    Transaction,
    DailyPlan,
    UserExtra,
    WeeklyPlan,
    EvalJobDescription,
    ResumePromptEval,
)
from app.models.bid_report_post import BidReportPost
from app.models.slack_alert_log import SlackAlertLog

DOCUMENT_MODELS = [
    UserExtra,
    Transaction,
    Account,
    DailyPlan,
    WeeklyPlan,
    Interview,
    InterviewChatLog,
    InterviewQuestion,
    Skill,
    AiReviewRun,
    AnalyzeChatLog,
    GlobalPrompt,
    ResumeApplication,
    ResumeLLMCache,
    SlackAlertLog,
    Notification,
    RecurringCharge,
    JobApplyRun,
    JobApplyRow,
    JobExtractionCache,
    JobInfo,
    JobApproval,
    JobInvalidRole,
    JobPostingCache,
    JobApplicationLog,
    Bidder,
    BidReport,
    BidReportPost,
    Bid,
    EvalJobDescription,
    ResumePromptEval,
    AiUsageEvent,
    Caller,
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


async def _migrate_tailored_map(database) -> None:
    """Job Applies rows linked one tailored resume (tailoredJobId/tailoredAccountId); now it's one per profile."""
    coll = database["job_apply_rows"]
    try:
        async for doc in coll.find({"tailoredJobId": {"$exists": True}}, {"tailoredJobId": 1, "tailoredAccountId": 1}):
            update: dict = {"$unset": {"tailoredJobId": "", "tailoredAccountId": ""}}
            if doc.get("tailoredJobId") and doc.get("tailoredAccountId"):
                update["$set"] = {f"tailored.{doc['tailoredAccountId']}": doc["tailoredJobId"]}
            await coll.update_one({"_id": doc["_id"]}, update)
    except Exception as exc:  # noqa: BLE001
        log.warning("tailored-map migration failed: %s", exc)


async def _migrate_log_filekey(database) -> None:
    """Applied-log entries from before tailored resumes have no fileKey; it equals their resumeId."""
    coll = database["job_application_log"]
    try:
        async for doc in coll.find({"fileKey": {"$exists": False}}, {"resumeId": 1}):
            await coll.update_one({"_id": doc["_id"]}, {"$set": {"fileKey": str(doc.get("resumeId"))}})
    except Exception as exc:  # noqa: BLE001
        log.warning("applied-log fileKey back-fill failed: %s", exc)


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


async def _migrate_job_apply_urls(database) -> None:
    """Re-clean stored Job Applies URLs when the cleaning rules change (app/job_apply/urls.py URL_VERSION), so
    applied history, the posting cache and run rows keep matching newly read sheets. Each document is marked
    with the version it was cleaned at, so this runs once per version. Two applied-history records (or cache
    entries) that become the same URL collapse to the first."""
    from pymongo.errors import DuplicateKeyError

    from app.job_apply.urls import URL_VERSION, normalize_url

    for name in ("job_application_log", "job_posting_cache", "job_apply_rows"):
        coll = database[name]
        try:
            async for doc in coll.find({"urlv": {"$ne": URL_VERSION}, "url": {"$type": "string"}}, {"url": 1}):
                clean = normalize_url(doc["url"])
                try:
                    await coll.update_one({"_id": doc["_id"]}, {"$set": {"url": clean, "urlv": URL_VERSION}})
                except DuplicateKeyError:
                    await coll.delete_one({"_id": doc["_id"]})  # the same job is already recorded in clean form
        except Exception as exc:  # noqa: BLE001
            log.warning("job-apply URL re-clean of %s failed: %s", name, exc)


async def _migrate_job_apply_screening(database) -> None:
    """Job Applies runs from before screening (no `phase`) keep working: they scored straight through, so they
    become auto-started score-phase runs with every group assigned to every selected profile, and their rows get
    the screening bucket and location group from what's already stored."""
    from app.job_apply.schema import GateResult, Location
    from app.job_apply.screening import bucket, group_key, markets_for

    runs, rows = database["job_apply_runs"], database["job_apply_rows"]
    try:
        async for run in runs.find({"phase": {"$exists": False}}, {"selection": 1}):
            ids = [s["accountId"] for s in run.get("selection") or [] if s.get("accountId")]
            await runs.update_one(
                {"_id": run["_id"]}, {"$set": {"phase": "score", "autoStart": True, "assignments": {"*": ids}}}
            )
            async for row in rows.find(
                {"runId": run["_id"], "screen": {"$exists": False}},
                {"status": 1, "statusReason": 1, "gates": 1, "extraction.allowedLocations": 1,
                 "extraction.isJobPosting": 1, "forceInclude": 1, "jdStructured": 1},
            ):
                ex = row.get("extraction")
                key = group_key([Location(**loc) for loc in ex.get("allowedLocations") or []]) if ex else None
                screen = bucket(
                    status=row.get("status") or "pending", status_reason=row.get("statusReason"),
                    gates=[GateResult(**g) for g in row.get("gates") or []], group_key=key,
                    force_include=bool(row.get("forceInclude")),
                    is_job=(ex or {}).get("isJobPosting") is not False,
                    verified=row.get("jdStructured") is not False,
                )
                await rows.update_one({"_id": row["_id"]}, {"$set": {"screen": screen, "groupKey": key}})
        # Jobs open only to candidates outside the target markets (US, UK/EU, Latam) are counted, not scored.
        # (a job a person approved stays Worth applying: it is scored with the "location not stated" picks)
        async for row in rows.find({"screen": {"$in": ["valid", "check"]}, "groupKey": {"$nin": [None, "none"]},
                                    "forceInclude": {"$ne": True}},
                                   {"groupKey": 1}):
            if not markets_for(row["groupKey"]):
                await rows.update_one({"_id": row["_id"]}, {"$set": {"screen": "other_location"}})
    except Exception as exc:  # noqa: BLE001
        log.warning("job-apply screening back-fill failed: %s", exc)


async def _migrate_run_retention(database) -> None:
    """Runs (and their rows) were kept 3 days: give the ones still here RUN_RETENTION_DAYS from their upload.
    Only ever lengthens; a second start finds nothing to do."""
    from app.models.job_apply import RUN_RETENTION_DAYS

    keep = RUN_RETENTION_DAYS * 24 * 3600 * 1000
    target = {"$add": ["$createdAt", keep]}
    try:
        for name in ("job_apply_runs", "job_apply_rows"):
            await database[name].update_many(
                {"createdAt": {"$type": "date"}, "$expr": {"$lt": ["$expiresAt", target]}},
                [{"$set": {"expiresAt": target}}],
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("job-apply retention back-fill failed: %s", exc)


async def init_db():
    global _client
    uri = os.environ["MONGODB_URI"]
    client = AsyncIOMotorClient(uri)
    _client = client
    database = client.get_default_database()
    await _migrate_log_filekey(database)
    await _migrate_tailored_map(database)
    await _drop_stale_indexes(database)
    await _migrate_legacy_resume_apps(database)
    await _migrate_default_leaderboard_optin(database)
    await _migrate_billing_cycle(database)
    await _migrate_legacy_payer_to_admin(database)
    await _migrate_job_apply_urls(database)
    await _migrate_job_apply_screening(database)
    await _migrate_run_retention(database)
    callers_new = "callers" not in await database.list_collection_names()
    await init_beanie(database=database, document_models=DOCUMENT_MODELS)
    if callers_new:  # the starting caller list, once: a list emptied later stays empty
        try:
            from app.routers.callers import seed_callers

            log.info("seeded %d callers", await seed_callers())
        except Exception as exc:  # noqa: BLE001
            log.warning("caller seed failed: %s", exc)
    try:
        from app.alerts_service import seed_recurring_charges_from_transactions

        seeded = await seed_recurring_charges_from_transactions()
        if seeded:
            log.info("seeded %d recurring charges from transactions", seeded)
    except Exception as exc:  # noqa: BLE001
        log.warning("recurring charge seed failed: %s", exc)
    try:
        from app.prompt_eval.seed_jds import seed_eval_jds

        inserted = await seed_eval_jds()
        if inserted:
            log.info("seeded %d eval job descriptions", inserted)
    except Exception as exc:  # noqa: BLE001
        log.warning("eval job description seed failed: %s", exc)
    return client
