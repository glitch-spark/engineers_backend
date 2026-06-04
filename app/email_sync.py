"""Gmail inbox sync — orchestrates: pull message IDs (initial date-bounded
list or history delta), fetch metadata, prefilter noise, classify the
remainder, persist EmailMessage rows, and (when confidence is high enough)
update the matched Application.

Idempotent: every batch uses the (accountId, messageId) unique index, so
re-running a sync just no-ops on already-stored messages.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from beanie import PydanticObjectId

from app.config import get_settings
from app.email_classifier import LABEL_TO_STAGE, classify_batch, is_obvious_noise
from app.email_crypto import decrypt
from app.gmail_api import (
    GmailApiError,
    fetch_userinfo,
    get_message_metadata,
    get_profile,
    list_history,
    list_message_ids,
    parse_message_metadata,
    refresh_access_token,
)
from app.models import Application, EmailAccount, EmailMessage
from app.models.application import KanbanStage, STAGE_ORDER, StageEvent

log = logging.getLogger(__name__)


# Concurrency for per-message metadata fetches. Gmail allows ~250 quota
# units/user/second; metadata.get costs 5 units. 10 in-flight ≈ 50 units/s
# average — well under the cap with headroom for the calling user's other
# clients (mobile, web).
_FETCH_CONCURRENCY = 10
_CLASSIFY_BATCH_SIZE = 20


async def _get_access_token(account: EmailAccount) -> str:
    """Decrypts the stored refresh token and refreshes a short-lived
    access token. Errors propagate so the caller can mark the account as
    requiring re-auth."""
    refresh_token = decrypt(account.refreshTokenEncrypted)
    return await refresh_access_token(refresh_token)


async def _fetch_metadata_batch(access_token: str, ids: list[str]) -> list[dict]:
    """Concurrent metadata.get with a small semaphore. Skips any message
    that 404s (deleted between list and get) — those are not errors."""
    sem = asyncio.Semaphore(_FETCH_CONCURRENCY)

    async def _one(mid: str) -> Optional[dict]:
        async with sem:
            try:
                raw = await get_message_metadata(access_token, mid)
            except GmailApiError as exc:
                if exc.status == 404:
                    return None
                raise
            return parse_message_metadata(raw)

    parsed = await asyncio.gather(*[_one(i) for i in ids])
    return [p for p in parsed if p]


async def _match_application(*, user_id: PydanticObjectId, company_guess: Optional[str]) -> Optional[Application]:
    """LLM-only matching per product spec: look up an existing Application
    by the classifier's companyName guess. No domain heuristic, no
    auto-create — unmatched messages land in Needs Review for the user
    to confirm."""
    if not company_guess:
        return None
    name = company_guess.strip().lower()
    if not name:
        return None
    return await Application.find_one(Application.userId == user_id, Application.companyNameLower == name)


def _stage_idx(stage: str) -> int:
    try:
        return STAGE_ORDER.index(stage)
    except ValueError:
        return len(STAGE_ORDER)


async def _apply_to_application(
    *,
    msg: EmailMessage,
    application: Application,
    auto_threshold: float,
) -> bool:
    """Mutates `application` in place. Returns True if it was modified.
    Forward-only stage advance for auto-events; outcome flips on offer /
    rejection regardless of stage rank because those are terminal."""
    changed = False
    if msg.id not in (application.emailMessageIds if hasattr(application, "emailMessageIds") else []):
        # Application doesn't track linked emails directly — we rely on the
        # reverse index (EmailMessage.applicationId) instead, so no field
        # change needed here. Left in case we add forward-link later.
        pass

    target = msg.targetStage
    label = msg.label

    if label == "offer" and msg.confidence >= auto_threshold:
        if application.outcome != "offer":
            application.outcome = "offer"
            changed = True
        target = KanbanStage.offer.value
    elif label == "rejection" and msg.confidence >= auto_threshold:
        if application.outcome != "rejected":
            application.outcome = "rejected"
            changed = True
        target = KanbanStage.rejected.value

    if target and msg.confidence >= auto_threshold:
        if _stage_idx(target) > _stage_idx(application.stage):
            application.stage = target
            application.stageHistory.append(
                StageEvent(stage=target, at=datetime.now(timezone.utc), source=f"email:{label}")
            )
            changed = True

    if changed:
        application.lastTouchedAt = datetime.now(timezone.utc)
    return changed


async def sync_account(account: EmailAccount) -> dict[str, int]:
    """Runs one sync pass. Picks delta if we have a historyId, otherwise
    the date-bounded initial pull. Returns a stats dict so the API caller
    can show counts to the user."""
    settings = get_settings()
    if account.disconnectedAt is not None:
        return {"fetched": 0, "classified": 0, "auto_applied": 0, "needs_review": 0}

    account.syncStatus = "running"
    account.lastSyncError = None
    await account.save()

    try:
        access_token = await _get_access_token(account)

        # --- determine which IDs to fetch ---
        message_ids: list[str] = []
        is_initial = not account.historyId
        if is_initial:
            since = datetime.now(timezone.utc) - timedelta(days=settings.EMAIL_INITIAL_SYNC_DAYS)
            query = f"in:inbox newer_than:{settings.EMAIL_INITIAL_SYNC_DAYS}d"
            page_token = None
            while len(message_ids) < settings.EMAIL_SYNC_MAX_MESSAGES:
                page = await list_message_ids(access_token, query=query, page_token=page_token, max_results=100)
                for m in page.get("messages") or []:
                    if m.get("id"):
                        message_ids.append(m["id"])
                page_token = page.get("nextPageToken")
                if not page_token:
                    break
            profile = await get_profile(access_token)
            new_history_id = str(profile.get("historyId") or "")
        else:
            try:
                page_token = None
                seen: set[str] = set()
                new_history_id = account.historyId
                while True:
                    page = await list_history(access_token, start_history_id=account.historyId, page_token=page_token)
                    for h in page.get("history") or []:
                        new_history_id = str(h.get("id") or new_history_id)
                        for added in h.get("messagesAdded") or []:
                            mid = (added.get("message") or {}).get("id")
                            if mid and mid not in seen:
                                seen.add(mid)
                                message_ids.append(mid)
                                if len(message_ids) >= settings.EMAIL_SYNC_MAX_MESSAGES:
                                    break
                        if len(message_ids) >= settings.EMAIL_SYNC_MAX_MESSAGES:
                            break
                    page_token = page.get("nextPageToken")
                    if not page_token or len(message_ids) >= settings.EMAIL_SYNC_MAX_MESSAGES:
                        break
            except GmailApiError as exc:
                # 404 = history cursor too old (>7 days). Fall back to a
                # short initial-style pull so we recover gracefully.
                if exc.status == 404:
                    log.info("history cursor expired for %s, falling back to date pull", account.email)
                    query = "in:inbox newer_than:2d"
                    page = await list_message_ids(access_token, query=query, max_results=100)
                    for m in page.get("messages") or []:
                        if m.get("id"):
                            message_ids.append(m["id"])
                    profile = await get_profile(access_token)
                    new_history_id = str(profile.get("historyId") or "")
                else:
                    raise

        # --- dedupe vs already-stored ---
        if message_ids:
            coll = EmailMessage.get_motor_collection()
            cursor = coll.find(
                {"accountId": account.id, "messageId": {"$in": message_ids}},
                {"messageId": 1},
            )
            stored = {doc["messageId"] async for doc in cursor}
            message_ids = [m for m in message_ids if m not in stored]

        if not message_ids:
            account.historyId = new_history_id or account.historyId
            account.lastSyncAt = datetime.now(timezone.utc)
            account.syncStatus = "idle"
            await account.save()
            return {"fetched": 0, "classified": 0, "auto_applied": 0, "needs_review": 0}

        # --- fetch metadata ---
        parsed = await _fetch_metadata_batch(access_token, message_ids)

        # --- split noise vs needs-classification ---
        noise: list[dict] = []
        for_llm: list[dict] = []
        for p in parsed:
            if is_obvious_noise(p["fromAddress"], p["subject"], p.get("isUnsub", False)):
                noise.append(p)
            else:
                for_llm.append(p)

        # --- classify in batches ---
        verdicts_by_id: dict[str, dict] = {}
        for i in range(0, len(for_llm), _CLASSIFY_BATCH_SIZE):
            chunk = for_llm[i : i + _CLASSIFY_BATCH_SIZE]
            inp = [
                {"id": m["messageId"], "from": m["fromAddress"], "subject": m["subject"], "snippet": m["snippet"]}
                for m in chunk
            ]
            results = await classify_batch(inp)
            for r in results:
                verdicts_by_id[r["id"]] = r

        # --- persist + apply ---
        auto_threshold = settings.EMAIL_AUTO_ADVANCE_CONFIDENCE
        auto_applied = 0
        needs_review = 0
        model_name = settings.EXTRACTION_MODEL

        for p in noise + for_llm:
            verdict = (
                {"label": "noise", "confidence": 1.0, "companyName": None}
                if p in noise
                else verdicts_by_id.get(p["messageId"], {"label": "follow_up", "confidence": 0.0, "companyName": None})
            )
            label = verdict["label"]
            confidence = float(verdict.get("confidence") or 0.0)
            company = verdict.get("companyName")
            target_stage = LABEL_TO_STAGE.get(label)

            msg = EmailMessage(
                userId=account.userId,
                accountId=account.id,
                messageId=p["messageId"],
                threadId=p["threadId"],
                historyId=p.get("historyId"),
                fromAddress=p["fromAddress"],
                fromName=p.get("fromName"),
                subject=p["subject"],
                snippet=p["snippet"],
                receivedAt=p["receivedAt"],
                label=label,
                confidence=confidence,
                companyGuess=company,
                targetStage=target_stage,
                classifierModel=model_name,
            )

            application: Optional[Application] = None
            if label in ("noise", "follow_up"):
                msg.reviewStatus = "ignored"
            else:
                application = await _match_application(user_id=account.userId, company_guess=company)
                if application is None:
                    msg.reviewStatus = "needs_review"
                    needs_review += 1
                else:
                    msg.applicationId = application.id
                    if confidence >= auto_threshold:
                        if await _apply_to_application(
                            msg=msg, application=application, auto_threshold=auto_threshold
                        ):
                            await application.save()
                        msg.reviewStatus = "auto_applied"
                        msg.reviewedAt = datetime.now(timezone.utc)
                        auto_applied += 1
                    else:
                        msg.reviewStatus = "needs_review"
                        needs_review += 1

            try:
                await msg.insert()
            except Exception as exc:  # noqa: BLE001
                # Likely the unique (accountId, messageId) index — concurrent
                # syncs racing. Safe to ignore.
                log.debug("email insert skipped (dup?): %s", exc)

        # --- finalize ---
        account.historyId = new_history_id or account.historyId
        account.lastSyncAt = datetime.now(timezone.utc)
        account.syncStatus = "idle"
        await account.save()
        return {
            "fetched": len(parsed),
            "classified": len(for_llm),
            "auto_applied": auto_applied,
            "needs_review": needs_review,
        }

    except GmailApiError as exc:
        log.warning("gmail sync failed for %s: %s", account.email, exc)
        account.syncStatus = "error"
        account.lastSyncError = exc.message[:500]
        await account.save()
        raise
    except Exception as exc:  # noqa: BLE001
        log.exception("gmail sync failed for %s: %s", account.email, exc)
        account.syncStatus = "error"
        account.lastSyncError = str(exc)[:500]
        await account.save()
        raise


async def bootstrap_account_from_oauth(
    *,
    user_id: PydanticObjectId,
    code: str,
) -> EmailAccount:
    """OAuth-callback helper: exchanges code → tokens, identifies the
    account email, upserts an EmailAccount row, and stores the encrypted
    refresh token. The first sync is left to the caller (we don't block
    the OAuth callback on a multi-minute mailbox pull)."""
    from app.email_crypto import encrypt
    from app.gmail_api import exchange_code

    token_response = await exchange_code(code)
    refresh_token = token_response.get("refresh_token")
    access_token = token_response.get("access_token")
    if not refresh_token:
        # Google omits refresh_token when the user previously authorized us
        # AND we forgot prompt=consent. We always pass it, so this should
        # not happen — fail loudly so we notice if the flow regresses.
        raise GmailApiError(400, "Google did not return a refresh_token — re-run OAuth with prompt=consent")
    if not access_token:
        raise GmailApiError(400, "Google did not return an access_token")

    userinfo = await fetch_userinfo(access_token)
    email = (userinfo.get("email") or "").lower().strip()
    if not email:
        raise GmailApiError(400, "Google did not return the account email")

    scopes = (token_response.get("scope") or "").split()

    existing = await EmailAccount.find_one(
        EmailAccount.userId == user_id,
        EmailAccount.email == email,
    )
    if existing is not None:
        existing.refreshTokenEncrypted = encrypt(refresh_token)
        existing.scopes = scopes
        existing.disconnectedAt = None
        existing.syncStatus = "idle"
        existing.lastSyncError = None
        await existing.save()
        return existing

    account = EmailAccount(
        userId=user_id,
        provider="gmail",
        email=email,
        refreshTokenEncrypted=encrypt(refresh_token),
        scopes=scopes,
    )
    await account.insert()
    return account
