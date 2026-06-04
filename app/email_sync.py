"""Gmail inbox sync orchestrator.

Pipeline per message:
  1. obvious-noise blocklist (free)        -> classify_email returns
  2. template hash → cache hit              -> reuse prior verdict
  3. rule engine sender+phrase match        -> deterministic label
  4. LLM extractor (single multi-output)    -> grounded label + evidence
  5. cross-checks (sender↔label sanity)     -> downgrade impossible pairs
  6. route by confidence                    -> auto-apply / needs-review / ignored

Anything that emerges with a label gets persisted with full audit
fields (sourceLayer + evidenceQuote + selfDoubt). The pipeline is
idempotent — re-running a sync on already-stored messages is a no-op.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from beanie import PydanticObjectId

from app.config import get_settings
from app.email_classifier import (
    ExtractedEmail,
    HIGH_STAKES_LABELS,
    LABEL_TO_STAGE,
    classify_batch,
    is_obvious_noise,
)
from app import email_provider
from app.email_crypto import decrypt
from app.email_provider import ProviderError
from app.email_rules import (
    cache_lookup,
    cache_store,
    find_matching_rule,
    sender_domain,
    template_hash,
)
from app.models import Application, EmailAccount, EmailMessage
from app.models.application import KanbanStage, STAGE_ORDER, StageEvent

log = logging.getLogger(__name__)


# Concurrency: Gmail allows ~250 quota units/user/sec; metadata.get costs 5.
_FETCH_CONCURRENCY = 10
_CLASSIFY_BATCH_SIZE = 15


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stage_idx(stage: str) -> int:
    try:
        return STAGE_ORDER.index(stage)
    except ValueError:
        return len(STAGE_ORDER)


async def _get_access_token(account: EmailAccount) -> str:
    refresh_token = decrypt(account.refreshTokenEncrypted)
    return await email_provider.refresh_access_token(account.provider, refresh_token)


async def _fetch_metadata_batch(account: EmailAccount, access_token: str, ids: list[str]) -> list[dict]:
    sem = asyncio.Semaphore(_FETCH_CONCURRENCY)

    async def _one(mid: str) -> Optional[dict]:
        async with sem:
            parsed = await email_provider.get_message_metadata(account, access_token, mid)
            return parsed or None

    parsed = await asyncio.gather(*[_one(i) for i in ids])
    return [p for p in parsed if p]


async def _fetch_body_batch(account: EmailAccount, access_token: str, ids: list[str]) -> dict[str, str]:
    """Concurrent body fetch — body is required for grounded classification.
    Missing/failed entries return empty string so the classifier still
    runs (it'll downgrade to noise for missing evidence)."""
    sem = asyncio.Semaphore(_FETCH_CONCURRENCY)

    async def _one(mid: str) -> tuple[str, str]:
        async with sem:
            try:
                body = await email_provider.fetch_message_body(account, access_token, mid)
            except ProviderError:
                body = ""
            return mid, body

    pairs = await asyncio.gather(*[_one(i) for i in ids])
    return dict(pairs)


# ---------------------------------------------------------------------------
# Cross-checks
# ---------------------------------------------------------------------------


_IMPOSSIBLE_SENDER_LABEL_PAIRS = [
    # sender_substr, forbidden labels
    ("noreply@", {"offer"}),
    ("no-reply@", {"offer"}),
    ("donotreply@", {"offer"}),
    ("@calendly.com", {"offer", "rejection"}),
    ("alerts@", {"applied", "offer", "rejection", "schedule_interview"}),
    ("digest@", {"applied", "offer", "rejection", "schedule_interview"}),
    ("newsletter@", {"applied", "offer", "rejection", "schedule_interview"}),
]


def _apply_cross_checks(addr: str, verdict: ExtractedEmail) -> ExtractedEmail:
    """Catches impossible (sender, label) pairs after the LLM. Cheap
    deterministic filter that prevents the worst class of misfires."""
    a = (addr or "").lower()
    for substr, forbidden in _IMPOSSIBLE_SENDER_LABEL_PAIRS:
        if substr in a and verdict.label in forbidden:
            return ExtractedEmail(
                label="noise",
                confidence=0.0,
                evidence_quote=verdict.evidence_quote,
                self_doubt=1.0,
                company_name=verdict.company_name,
                stage_round=None,
                scheduled_at=None,
                interviewer_name=None,
            )
    return verdict


# ---------------------------------------------------------------------------
# Per-message decision
# ---------------------------------------------------------------------------


async def classify_email(
    *, from_addr: str, subject: str, body: str, gmail_labels: Optional[list[str]] = None,
    list_unsubscribe: bool = False,
) -> tuple[ExtractedEmail, str]:
    """Returns (verdict, source_layer). Source layer values:
      'noise_blocklist' | 'cache' | 'rule_engine' | 'llm' | 'cross_check'.

    All layers feed the SAME ExtractedEmail shape — no callers need to
    branch on which one fired."""
    # 1. Obvious noise
    if is_obvious_noise(from_addr, subject, list_unsubscribe=list_unsubscribe, gmail_labels=gmail_labels):
        return (
            ExtractedEmail(
                label="noise", confidence=1.0, evidence_quote="(blocked sender)",
                self_doubt=0.0, company_name=None, stage_round=None,
                scheduled_at=None, interviewer_name=None,
            ),
            "noise_blocklist",
        )

    # 2. Cache
    th = template_hash(from_addr=from_addr, subject=subject, body_or_snippet=body)
    cached = await cache_lookup(th)
    if cached is not None:
        return (
            ExtractedEmail(
                label=cached.label, confidence=cached.confidence,
                evidence_quote=cached.evidenceQuote or "",
                self_doubt=0.0, company_name=cached.companyHint,
                stage_round=cached.stageRound, scheduled_at=None,
                interviewer_name=None,
            ),
            "cache",
        )

    # 3. Rule engine
    rule = await find_matching_rule(from_addr=from_addr, subject=subject, body=body)
    if rule is not None:
        verdict = ExtractedEmail(
            label=rule.label, confidence=rule.confidence,
            evidence_quote=rule.phrasePattern, self_doubt=0.0,
            company_name=None,
            stage_round=rule.stageRound or LABEL_TO_STAGE.get(rule.label),
            scheduled_at=None, interviewer_name=None,
        )
        await cache_store(
            tpl_hash=th, label=verdict.label, confidence=verdict.confidence,
            stage_round=verdict.stage_round, company_hint=verdict.company_name,
            evidence_quote=verdict.evidence_quote,
            sender_domain_str=sender_domain(from_addr),
            subject_preview=subject[:120],
        )
        return verdict, "rule_engine"

    # 4. LLM (caller batches; single-msg fallback path)
    results = await classify_batch([{"id": th, "from": from_addr, "subject": subject, "body": body}])
    verdict = results.get(th)
    if verdict is None:
        return (
            ExtractedEmail(
                label="follow_up", confidence=0.0, evidence_quote="",
                self_doubt=1.0, company_name=None, stage_round=None,
                scheduled_at=None, interviewer_name=None,
            ),
            "llm",
        )

    # 5. Cross-checks
    verdict = _apply_cross_checks(from_addr, verdict)

    # Cache the LLM verdict for future templates.
    await cache_store(
        tpl_hash=th, label=verdict.label, confidence=verdict.confidence,
        stage_round=verdict.stage_round, company_hint=verdict.company_name,
        evidence_quote=verdict.evidence_quote,
        sender_domain_str=sender_domain(from_addr),
        subject_preview=subject[:120],
    )
    return verdict, "llm"


# ---------------------------------------------------------------------------
# Application linkage
# ---------------------------------------------------------------------------


async def _match_application(
    *, user_id: PydanticObjectId, company_guess: Optional[str]
) -> Optional[Application]:
    """LLM-only matching: classifier's companyName guess → existing
    Application by companyNameLower. No domain heuristics. No
    auto-create — unmatched goes to Needs Review."""
    if not company_guess:
        return None
    name = company_guess.strip().lower()
    if not name:
        return None
    return await Application.find_one(
        Application.userId == user_id, Application.companyNameLower == name
    )


async def _apply_to_application(
    *, verdict: ExtractedEmail, application: Application,
) -> bool:
    changed = False
    target = verdict.stage_round or LABEL_TO_STAGE.get(verdict.label)

    if verdict.label == "offer":
        if application.outcome != "offer":
            application.outcome = "offer"
            changed = True
        target = KanbanStage.offer.value
    elif verdict.label == "rejection":
        if application.outcome != "rejected":
            application.outcome = "rejected"
            changed = True
        target = KanbanStage.rejected.value

    if target and _stage_idx(target) > _stage_idx(application.stage):
        application.stage = target
        application.stageHistory.append(
            StageEvent(
                stage=target, at=datetime.now(timezone.utc),
                source=f"email:{verdict.label}",
            )
        )
        changed = True

    if changed:
        application.lastTouchedAt = datetime.now(timezone.utc)
    return changed


# ---------------------------------------------------------------------------
# Sync entry point
# ---------------------------------------------------------------------------


async def sync_account(account: EmailAccount) -> dict[str, int]:
    settings = get_settings()
    if account.disconnectedAt is not None:
        return {"fetched": 0, "classified": 0, "auto_applied": 0, "needs_review": 0}

    account.syncStatus = "running"
    account.lastSyncError = None
    await account.save()

    try:
        access_token = await _get_access_token(account)

        # --- pick message IDs (provider-agnostic) ---
        is_initial = not account.historyId
        if is_initial:
            message_ids, new_history_id = await email_provider.list_initial_message_ids(
                account,
                access_token=access_token,
                days=settings.EMAIL_INITIAL_SYNC_DAYS,
                max_results=settings.EMAIL_SYNC_MAX_MESSAGES,
            )
        else:
            message_ids, new_history_id, expired = await email_provider.list_delta_message_ids(
                account,
                access_token=access_token,
                cursor=account.historyId,
                max_results=settings.EMAIL_SYNC_MAX_MESSAGES,
            )
            if expired:
                log.info("delta cursor expired for %s, falling back to short initial pull", account.email)
                message_ids, new_history_id = await email_provider.list_initial_message_ids(
                    account, access_token=access_token, days=2,
                    max_results=settings.EMAIL_SYNC_MAX_MESSAGES,
                )

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
        parsed = await _fetch_metadata_batch(account, access_token, message_ids)

        # --- bucket by short-circuit (blocklist / rule / cache) vs needs-LLM ---
        # First do the cheap async-without-body checks: blocklist, then
        # template-hash cache, then rule engine (using snippet for body).
        verdicts: dict[str, tuple[ExtractedEmail, str]] = {}
        needs_llm: list[dict] = []
        for p in parsed:
            mid = p["messageId"]
            # We may not have body yet; rule + cache use subject + snippet.
            if is_obvious_noise(p["fromAddress"], p["subject"], list_unsubscribe=p.get("isUnsub", False)):
                verdicts[mid] = (
                    ExtractedEmail(
                        label="noise", confidence=1.0,
                        evidence_quote="(blocked sender)", self_doubt=0.0,
                        company_name=None, stage_round=None,
                        scheduled_at=None, interviewer_name=None,
                    ),
                    "noise_blocklist",
                )
                continue
            th = template_hash(from_addr=p["fromAddress"], subject=p["subject"], body_or_snippet=p["snippet"])
            cached = await cache_lookup(th)
            if cached is not None:
                verdicts[mid] = (
                    ExtractedEmail(
                        label=cached.label, confidence=cached.confidence,
                        evidence_quote=cached.evidenceQuote or "",
                        self_doubt=0.0, company_name=cached.companyHint,
                        stage_round=cached.stageRound, scheduled_at=None,
                        interviewer_name=None,
                    ),
                    "cache",
                )
                continue
            rule = await find_matching_rule(
                from_addr=p["fromAddress"], subject=p["subject"], body=p["snippet"]
            )
            if rule is not None:
                verdict = ExtractedEmail(
                    label=rule.label, confidence=rule.confidence,
                    evidence_quote=rule.phrasePattern, self_doubt=0.0,
                    company_name=None,
                    stage_round=rule.stageRound or LABEL_TO_STAGE.get(rule.label),
                    scheduled_at=None, interviewer_name=None,
                )
                verdicts[mid] = (verdict, "rule_engine")
                await cache_store(
                    tpl_hash=th, label=verdict.label, confidence=verdict.confidence,
                    stage_round=verdict.stage_round, company_hint=verdict.company_name,
                    evidence_quote=verdict.evidence_quote,
                    sender_domain_str=sender_domain(p["fromAddress"]),
                    subject_preview=p["subject"][:120],
                )
                continue
            needs_llm.append(p)

        # --- fetch bodies for LLM bucket ---
        if needs_llm:
            bodies = await _fetch_body_batch(account, access_token, [p["messageId"] for p in needs_llm])
            for p in needs_llm:
                p["body"] = bodies.get(p["messageId"], "")

        # --- classify LLM bucket in batches ---
        for i in range(0, len(needs_llm), _CLASSIFY_BATCH_SIZE):
            chunk = needs_llm[i : i + _CLASSIFY_BATCH_SIZE]
            inputs = [
                {
                    "id": m["messageId"],
                    "from": m["fromAddress"],
                    "subject": m["subject"],
                    "body": m.get("body") or m["snippet"],
                }
                for m in chunk
            ]
            llm_results = await classify_batch(inputs)
            for m in chunk:
                v = llm_results.get(m["messageId"])
                if v is None:
                    v = ExtractedEmail(
                        label="follow_up", confidence=0.0, evidence_quote="",
                        self_doubt=1.0, company_name=None, stage_round=None,
                        scheduled_at=None, interviewer_name=None,
                    )
                v = _apply_cross_checks(m["fromAddress"], v)
                verdicts[m["messageId"]] = (v, "llm")
                # Cache the LLM verdict against the body-aware hash.
                th = template_hash(
                    from_addr=m["fromAddress"], subject=m["subject"],
                    body_or_snippet=m.get("body") or m["snippet"],
                )
                await cache_store(
                    tpl_hash=th, label=v.label, confidence=v.confidence,
                    stage_round=v.stage_round, company_hint=v.company_name,
                    evidence_quote=v.evidence_quote,
                    sender_domain_str=sender_domain(m["fromAddress"]),
                    subject_preview=m["subject"][:120],
                )

        # --- persist + auto-apply ---
        auto_threshold = settings.EMAIL_AUTO_ADVANCE_CONFIDENCE
        auto_applied = 0
        needs_review = 0
        model_name = settings.EXTRACTION_MODEL

        for p in parsed:
            mid = p["messageId"]
            verdict, source = verdicts.get(mid, (
                ExtractedEmail(
                    label="follow_up", confidence=0.0, evidence_quote="",
                    self_doubt=1.0, company_name=None, stage_round=None,
                    scheduled_at=None, interviewer_name=None,
                ),
                "llm",
            ))

            scheduled_at_dt: Optional[datetime] = None
            if verdict.scheduled_at:
                try:
                    scheduled_at_dt = datetime.fromisoformat(verdict.scheduled_at.replace("Z", "+00:00"))
                except ValueError:
                    scheduled_at_dt = None

            msg = EmailMessage(
                userId=account.userId,
                accountId=account.id,
                messageId=mid,
                threadId=p["threadId"],
                historyId=p.get("historyId"),
                fromAddress=p["fromAddress"],
                fromName=p.get("fromName"),
                subject=p["subject"],
                snippet=p["snippet"],
                receivedAt=p["receivedAt"],
                label=verdict.label,
                confidence=verdict.confidence,
                companyGuess=verdict.company_name,
                targetStage=verdict.stage_round,
                classifierModel=model_name,
                evidenceQuote=verdict.evidence_quote,
                sourceLayer=source,
                selfDoubt=verdict.self_doubt,
                scheduledAt=scheduled_at_dt,
                interviewerName=verdict.interviewer_name,
            )

            if verdict.label in ("noise", "follow_up"):
                msg.reviewStatus = "ignored"
            else:
                application = await _match_application(
                    user_id=account.userId, company_guess=verdict.company_name
                )
                if application is None:
                    msg.reviewStatus = "needs_review"
                    needs_review += 1
                else:
                    msg.applicationId = application.id
                    if verdict.confidence >= auto_threshold:
                        if await _apply_to_application(verdict=verdict, application=application):
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
                log.debug("email insert skipped (dup?): %s", exc)

        account.historyId = new_history_id or account.historyId
        account.lastSyncAt = datetime.now(timezone.utc)
        account.syncStatus = "idle"
        await account.save()
        return {
            "fetched": len(parsed),
            "classified": len(needs_llm),
            "auto_applied": auto_applied,
            "needs_review": needs_review,
        }

    except ProviderError as exc:
        log.warning("email sync failed for %s (%s): %s", account.email, account.provider, exc)
        account.syncStatus = "error"
        account.lastSyncError = exc.message[:500]
        await account.save()
        raise
    except Exception as exc:  # noqa: BLE001
        log.exception("email sync failed for %s (%s): %s", account.email, account.provider, exc)
        account.syncStatus = "error"
        account.lastSyncError = str(exc)[:500]
        await account.save()
        raise


# ---------------------------------------------------------------------------
# OAuth bootstrap — provider-agnostic
# ---------------------------------------------------------------------------


async def bootstrap_account_from_oauth(
    *, user_id: PydanticObjectId, provider: str, code: str,
) -> EmailAccount:
    from app.email_crypto import encrypt

    token_response = await email_provider.exchange_code(provider, code)
    refresh_token = token_response.get("refresh_token")
    access_token = token_response.get("access_token")
    if not refresh_token:
        raise ProviderError(400, f"{provider} did not return a refresh_token — re-run OAuth with prompt=consent")
    if not access_token:
        raise ProviderError(400, f"{provider} did not return an access_token")

    userinfo = await email_provider.fetch_userinfo(provider, access_token)
    email = (userinfo.get("email") or "").lower().strip()
    if not email:
        raise ProviderError(400, f"{provider} did not return the account email")

    scopes = (token_response.get("scope") or "").split()

    existing = await EmailAccount.find_one(
        EmailAccount.userId == user_id,
        EmailAccount.email == email,
    )
    if existing is not None:
        existing.provider = provider  # type: ignore[assignment]
        existing.refreshTokenEncrypted = encrypt(refresh_token)
        existing.scopes = scopes
        existing.disconnectedAt = None
        existing.syncStatus = "idle"
        existing.lastSyncError = None
        await existing.save()
        return existing

    account = EmailAccount(
        userId=user_id, provider=provider, email=email,  # type: ignore[arg-type]
        refreshTokenEncrypted=encrypt(refresh_token), scopes=scopes,
    )
    await account.insert()
    return account
