"""Email integrations router — Gmail OAuth + sync + needs-review actions.

Routes:
  GET    /integrations/email/accounts                  list user's connected accounts
  POST   /integrations/email/gmail/oauth-start         returns Google consent URL
  GET    /integrations/email/gmail/oauth-callback      redirect target Google sends back to
  POST   /integrations/email/{id}/sync                 manual delta sync (or initial)
  DELETE /integrations/email/{id}                      revoke + soft-delete the account
  GET    /integrations/email/messages                  list classified messages (review queue)
  POST   /integrations/email/messages/{id}/apply       confirm proposed action
  POST   /integrations/email/messages/{id}/dismiss     drop from review queue

The OAuth callback uses a short-lived signed `state` token to bind the
flow back to the originating user. We deliberately avoid sessions/cookies
since the frontend stores its JWT in memory only.
"""

from __future__ import annotations

import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Annotated, Any, List, Optional
from urllib.parse import urlencode

from beanie import PydanticObjectId
from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import RedirectResponse
from jose import JWTError, jwt
from pydantic import BaseModel

from app.config import get_settings
from app.deps import get_current_user
from app.email_classifier import LABEL_TO_STAGE
from app.email_crypto import EmailCryptoUnavailable
from app import email_provider
from app.email_provider import ProviderError
from app.email_sync import bootstrap_account_from_oauth, sync_account
from app.models import Application, EmailAccount, EmailMessage, EmailRule, UserExtra
from app.email_rules import sender_domain
from app.models.application import KanbanStage, STAGE_ORDER, StageEvent
from app.models.email_account import EmailAccountPublic
from app.models.email_message import EmailMessagePublic
from app.models.email_rule import EmailRulePublic
from app.routers._util import to_object_id

log = logging.getLogger(__name__)

router = APIRouter(prefix="/integrations/email", tags=["integrations"])

# Google's OAuth client config (and the matching env var) places the
# redirect URI at /integrations/gmail/oauth-callback — without the
# /email/ segment. Rather than force every operator to update Google
# Cloud + .env, we expose the callback under a second router at that
# bare path. All other email routes stay under /integrations/email.
oauth_router = APIRouter(prefix="/integrations/gmail", tags=["integrations"])

# Microsoft Identity uses its own redirect URI (configured in the Azure
# AD app registration). Same rationale as the gmail oauth_router — keep
# the callback at the URI Microsoft was registered with, regardless of
# the email-router prefix layout.
outlook_oauth_router = APIRouter(prefix="/integrations/outlook", tags=["integrations"])


# ---- state token signing ---------------------------------------------------
# Re-use the JWT signing key already used for auth tokens. State carries
# {userId, nonce, exp} so the callback can attribute the OAuth code back
# to the user that initiated the flow.

_STATE_AUDIENCE = "gmail-oauth"
_STATE_TTL_SECONDS = 600  # 10 min — generous for slow consent flows


def _sign_state(user_id: PydanticObjectId) -> str:
    s = get_settings()
    payload = {
        "sub": str(user_id),
        "aud": _STATE_AUDIENCE,
        "exp": datetime.now(timezone.utc) + timedelta(seconds=_STATE_TTL_SECONDS),
        "jti": secrets.token_urlsafe(16),
    }
    return jwt.encode(payload, s.AUTH_SECRET, algorithm="HS256")


def _verify_state(token: str) -> PydanticObjectId:
    s = get_settings()
    try:
        payload = jwt.decode(token, s.AUTH_SECRET, algorithms=["HS256"], audience=_STATE_AUDIENCE)
        return PydanticObjectId(payload["sub"])
    except (JWTError, KeyError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="invalid or expired state") from exc


def _frontend_redirect(*, status_value: str, detail: Optional[str] = None) -> RedirectResponse:
    """OAuth callback redirects the browser back to the SPA with a status
    query param. The SPA reads it and shows toast / closes the popup."""
    s = get_settings()
    qs = {"status": status_value}
    if detail:
        qs["detail"] = detail[:200]
    return RedirectResponse(url=f"{s.FRONTEND_ORIGIN}/integrations?{urlencode(qs)}", status_code=302)


# ---------------------------------------------------------------------------
# Account endpoints
# ---------------------------------------------------------------------------


@router.get("/accounts")
async def list_accounts(user: Annotated[UserExtra, Depends(get_current_user)]) -> dict:
    rows = await EmailAccount.find(EmailAccount.userId == user.id).sort([("createdAt", -1)]).to_list()
    return {"accounts": [EmailAccountPublic.from_doc(r).model_dump() for r in rows]}


def _oauth_start_for(provider: str, user_id) -> str:
    s = get_settings()
    if not s.EMAIL_TOKEN_KEY:
        raise HTTPException(status_code=503, detail="EMAIL_TOKEN_KEY is not configured")
    if provider == "gmail" and not s.GOOGLE_OAUTH_CLIENT_ID:
        raise HTTPException(status_code=503, detail="Gmail OAuth is not configured on this server")
    if provider == "outlook" and not s.MS_OAUTH_CLIENT_ID:
        raise HTTPException(status_code=503, detail="Outlook OAuth is not configured on this server")
    state = _sign_state(user_id)
    # No login_hint for either provider: we want the account chooser so the
    # user can connect ANY Gmail/Outlook account rather than being pinned to
    # their app login. The chooser is forced via prompt=select_account in each
    # provider's builder.
    try:
        return email_provider.build_authorization_url(provider, state=state)
    except ProviderError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.message) from exc


@router.post("/gmail/oauth-start")
async def gmail_oauth_start(user: Annotated[UserExtra, Depends(get_current_user)]) -> dict:
    return {"url": _oauth_start_for("gmail", user.id)}


@router.post("/outlook/oauth-start")
async def outlook_oauth_start(user: Annotated[UserExtra, Depends(get_current_user)]) -> dict:
    return {"url": _oauth_start_for("outlook", user.id)}


async def _handle_oauth_callback(
    provider: str, code: Optional[str], state: Optional[str], error: Optional[str],
) -> RedirectResponse:
    if error:
        return _frontend_redirect(status_value="error", detail=error)
    if not code or not state:
        return _frontend_redirect(status_value="error", detail="missing code or state")
    try:
        user_id = _verify_state(state)
    except HTTPException as exc:
        return _frontend_redirect(status_value="error", detail=str(exc.detail))
    try:
        account = await bootstrap_account_from_oauth(user_id=user_id, provider=provider, code=code)
    except (ProviderError, EmailCryptoUnavailable) as exc:
        log.warning("%s oauth-callback failed for %s: %s", provider, user_id, exc)
        return _frontend_redirect(status_value="error", detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        log.exception("%s oauth-callback unexpected error", provider)
        return _frontend_redirect(status_value="error", detail="internal error")
    return _frontend_redirect(status_value="connected", detail=account.email)


@oauth_router.get("/oauth-callback")
async def gmail_oauth_callback(
    code: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
) -> RedirectResponse:
    return await _handle_oauth_callback("gmail", code, state, error)


@outlook_oauth_router.get("/oauth-callback")
async def outlook_oauth_callback(
    code: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
) -> RedirectResponse:
    return await _handle_oauth_callback("outlook", code, state, error)


@router.delete("/{account_id}", status_code=204)
async def disconnect_account(
    account_id: str,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> None:
    oid = to_object_id(account_id)
    account = await EmailAccount.get(oid)
    if account is None or account.userId != user.id:
        raise HTTPException(status_code=404, detail="not found")
    # Best-effort revoke upstream, then wipe the encrypted token locally so
    # even with the file backup an attacker has no usable credential.
    try:
        from app.email_crypto import decrypt
        token = decrypt(account.refreshTokenEncrypted)
        await email_provider.revoke_refresh_token(account.provider, token)
    except Exception as exc:  # noqa: BLE001
        log.warning("revoke during disconnect failed for %s: %s", account.email, exc)
    account.refreshTokenEncrypted = ""  # wipe
    account.disconnectedAt = datetime.now(timezone.utc)
    account.syncStatus = "idle"
    await account.save()


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------


# Per-user rate limit on manual sync. Cheap in-memory map — multi-instance
# deployments will allow one sync per replica, which is fine; the worker
# itself is idempotent.
_LAST_SYNC: dict[str, datetime] = {}
_SYNC_COOLDOWN_SECONDS = 30


@router.post("/{account_id}/reset")
async def reset_sync(
    account_id: str,
    user: Annotated[UserExtra, Depends(get_current_user)],
    fullReSync: bool = Query(False, description="also clear historyId so the next sync re-pulls the full N-day window"),
) -> dict:
    """Unsticks an account whose sync got stuck in `running` (interrupted
    process) and optionally wipes the history cursor so the next click
    runs a fresh initial pull. Clears the local cooldown record too."""
    oid = to_object_id(account_id)
    account = await EmailAccount.get(oid)
    if account is None or account.userId != user.id:
        raise HTTPException(status_code=404, detail="not found")
    account.syncStatus = "idle"
    account.lastSyncError = None
    if fullReSync:
        account.historyId = None
    await account.save()
    _LAST_SYNC.pop(account_id, None)
    return {
        "ok": True,
        "syncStatus": account.syncStatus,
        "historyId": account.historyId,
    }


@router.post("/{account_id}/sync")
async def sync(
    account_id: str,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    oid = to_object_id(account_id)
    account = await EmailAccount.get(oid)
    if account is None or account.userId != user.id:
        raise HTTPException(status_code=404, detail="not found")
    if account.disconnectedAt is not None:
        raise HTTPException(status_code=400, detail="account is disconnected — reconnect first")
    if account.syncStatus == "running":
        raise HTTPException(status_code=409, detail="sync already running")

    last = _LAST_SYNC.get(account_id)
    if last and (datetime.now(timezone.utc) - last).total_seconds() < _SYNC_COOLDOWN_SECONDS:
        raise HTTPException(status_code=429, detail=f"Please wait {_SYNC_COOLDOWN_SECONDS}s between syncs")
    _LAST_SYNC[account_id] = datetime.now(timezone.utc)

    try:
        stats = await sync_account(account)
    except ProviderError as exc:
        # 400 invalid_grant = user revoked us; surface as 401 so the SPA
        # prompts a reconnect rather than retrying forever.
        if exc.status in (400, 401):
            raise HTTPException(status_code=401, detail="Authorization expired — reconnect required") from exc
        raise HTTPException(status_code=502, detail=exc.message) from exc
    return {"ok": True, "stats": stats}


# ---------------------------------------------------------------------------
# Messages / review queue
# ---------------------------------------------------------------------------


@router.get("/messages")
async def list_messages(
    user: Annotated[UserExtra, Depends(get_current_user)],
    accountId: Optional[str] = Query(None),
    applicationId: Optional[str] = Query(None),
    reviewStatus: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
) -> dict:
    """Filterable list. `reviewStatus=needs_review` powers the Inbox tab on
    the Pipeline page; `applicationId` powers the drawer email list."""
    q: dict[str, Any] = {"userId": user.id}
    if accountId:
        q["accountId"] = to_object_id(accountId)
    if applicationId:
        q["applicationId"] = to_object_id(applicationId)
    if reviewStatus:
        q["reviewStatus"] = reviewStatus
    rows = await EmailMessage.find(q).sort([("receivedAt", -1)]).limit(limit).to_list()
    return {"messages": [EmailMessagePublic.from_doc(r).model_dump() for r in rows]}


class ApplyMessageBody(BaseModel):
    applicationId: Optional[str] = None
    stage: Optional[str] = None


@router.post("/messages/{message_id}/apply")
async def apply_message(
    message_id: str,
    body: ApplyMessageBody,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    """User-confirmed action from the review tray. Either:
      - links to applicationId + advances stage (defaulting to targetStage),
      - or just links to applicationId without advancing if stage omitted."""
    oid = to_object_id(message_id)
    msg = await EmailMessage.get(oid)
    if msg is None or msg.userId != user.id:
        raise HTTPException(status_code=404, detail="not found")

    app_id = body.applicationId or (str(msg.applicationId) if msg.applicationId else None)
    if not app_id:
        raise HTTPException(status_code=400, detail="applicationId is required")
    app = await Application.get(to_object_id(app_id))
    if app is None or app.userId != user.id:
        raise HTTPException(status_code=404, detail="application not found")

    target_stage = body.stage or msg.targetStage
    valid_stages = {s.value for s in KanbanStage}
    if target_stage and target_stage in valid_stages:
        # forward-only advance
        try:
            old_idx = STAGE_ORDER.index(app.stage)
        except ValueError:
            old_idx = len(STAGE_ORDER)
        try:
            new_idx = STAGE_ORDER.index(target_stage)
        except ValueError:
            new_idx = len(STAGE_ORDER)
        if new_idx > old_idx:
            app.stage = target_stage
            app.stageHistory.append(
                StageEvent(stage=target_stage, at=datetime.now(timezone.utc), by=user.id, source=f"email-confirm:{msg.label}")
            )
        if target_stage == KanbanStage.offer.value:
            app.outcome = "offer"
        elif target_stage == KanbanStage.rejected.value:
            app.outcome = "rejected"
        app.lastTouchedAt = datetime.now(timezone.utc)
        await app.save()

    msg.applicationId = app.id
    msg.reviewStatus = "applied"
    msg.reviewedAt = datetime.now(timezone.utc)
    msg.reviewedBy = user.id
    await msg.save()

    # Learn from the correction. If the classifier label was uncertain or
    # wrong, the user's confirmation becomes a deterministic rule for the
    # same (sender domain, evidence phrase) combination next time.
    await _learn_rule_from_confirmation(msg=msg, user_id=user.id)
    return {"ok": True}


async def _learn_rule_from_confirmation(
    *, msg: EmailMessage, user_id: PydanticObjectId,
) -> None:
    """Writes an EmailRule from a user's confirmation in the review tray.

    Pattern strategy:
      - senderPattern: '@' + the sender's domain. This is broader than
        the exact address (one address per recruiter is normal), but
        narrower than top-level domain.
      - phrasePattern: msg.evidenceQuote if it looks reasonable
        (15-200 chars, no obvious recipient personalization like the
        user's name). Fallback to a stable substring of the subject.

    Skipped if a rule with this exact (sender, phrase) already exists.
    Idempotent on re-confirmation.
    """
    domain = sender_domain(msg.fromAddress)
    if not domain or len(domain) < 4:
        return
    label = msg.label or ""
    if label in ("", "noise", "follow_up"):
        return
    phrase = (msg.evidenceQuote or "").strip().lower()
    if not (15 <= len(phrase) <= 200):
        phrase = (msg.subject or "").strip().lower()[:120]
    if len(phrase) < 10:
        return
    try:
        existing = await EmailRule.find_one(
            EmailRule.senderPattern == domain,
            EmailRule.phrasePattern == phrase,
        )
        if existing is not None:
            existing.hitCount += 1
            existing.lastHitAt = datetime.now(timezone.utc)
            existing.label = label
            existing.stageRound = msg.targetStage
            await existing.save()
            return
        await EmailRule(
            senderPattern=domain,
            phrasePattern=phrase,
            label=label,
            stageRound=msg.targetStage,
            confidence=0.95,  # slightly below seed rules — user-derived
            source="user_correction",
            createdBy=user_id,
            notes=f"learned from message {msg.id}",
        ).insert()
    except Exception as exc:  # noqa: BLE001
        log.warning("learn-rule from confirmation failed: %s", exc)


@router.post("/messages/{message_id}/dismiss")
async def dismiss_message(
    message_id: str,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    oid = to_object_id(message_id)
    msg = await EmailMessage.get(oid)
    if msg is None or msg.userId != user.id:
        raise HTTPException(status_code=404, detail="not found")
    msg.reviewStatus = "dismissed"
    msg.reviewedAt = datetime.now(timezone.utc)
    msg.reviewedBy = user.id
    await msg.save()
    return {"ok": True}


@router.get("/messages/{message_id}/body")
async def message_body(
    message_id: str,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    """On-demand body fetch — body is NEVER persisted server-side. Hits
    Gmail directly each request. The drawer calls this when opened."""
    from app.email_crypto import decrypt
    oid = to_object_id(message_id)
    msg = await EmailMessage.get(oid)
    if msg is None or msg.userId != user.id:
        raise HTTPException(status_code=404, detail="not found")
    account = await EmailAccount.get(msg.accountId)
    if account is None or account.disconnectedAt is not None:
        raise HTTPException(status_code=400, detail="account disconnected")
    try:
        refresh = decrypt(account.refreshTokenEncrypted)
        access = await email_provider.refresh_access_token(account.provider, refresh)
        body = await email_provider.fetch_message_body(account, access, msg.messageId)
    except ProviderError as exc:
        raise HTTPException(status_code=502, detail=exc.message) from exc
    return {"body": body}


# ---------------------------------------------------------------------------
# Rule management (admin)
# ---------------------------------------------------------------------------


class RuleUpsertBody(BaseModel):
    senderPattern: str
    phrasePattern: str
    label: str
    stageRound: Optional[str] = None
    confidence: float = 1.0
    notes: str = ""
    enabled: bool = True


@router.get("/rules")
async def list_rules(
    user: Annotated[UserExtra, Depends(get_current_user)],
    label: Optional[str] = Query(None),
    source: Optional[str] = Query(None),
    enabledOnly: bool = Query(False),
    limit: int = Query(500, ge=1, le=2000),
) -> dict:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    q: dict[str, Any] = {}
    if label:
        q["label"] = label
    if source:
        q["source"] = source
    if enabledOnly:
        q["enabled"] = True
    rows = await EmailRule.find(q).sort([("hitCount", -1)]).limit(limit).to_list()
    return {"rules": [EmailRulePublic.from_doc(r).model_dump() for r in rows]}


@router.post("/rules")
async def create_rule(
    body: RuleUpsertBody,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    sp = body.senderPattern.strip().lower()
    pp = body.phrasePattern.strip().lower()
    if not sp or not pp:
        raise HTTPException(status_code=400, detail="senderPattern and phrasePattern are required")
    existing = await EmailRule.find_one(
        EmailRule.senderPattern == sp, EmailRule.phrasePattern == pp,
    )
    if existing is not None:
        existing.label = body.label
        existing.stageRound = body.stageRound
        existing.confidence = body.confidence
        existing.notes = body.notes or existing.notes
        existing.enabled = body.enabled
        await existing.save()
        return EmailRulePublic.from_doc(existing).model_dump()
    row = EmailRule(
        senderPattern=sp, phrasePattern=pp, label=body.label,
        stageRound=body.stageRound, confidence=body.confidence,
        notes=body.notes, enabled=body.enabled,
        source="admin_added", createdBy=user.id,
    )
    await row.insert()
    return EmailRulePublic.from_doc(row).model_dump()


@router.patch("/rules/{rule_id}")
async def update_rule(
    rule_id: str,
    body: RuleUpsertBody,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> dict:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    row = await EmailRule.get(to_object_id(rule_id))
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    row.senderPattern = body.senderPattern.strip().lower()
    row.phrasePattern = body.phrasePattern.strip().lower()
    row.label = body.label
    row.stageRound = body.stageRound
    row.confidence = body.confidence
    row.notes = body.notes
    row.enabled = body.enabled
    await row.save()
    return EmailRulePublic.from_doc(row).model_dump()


@router.delete("/rules/{rule_id}", status_code=204)
async def delete_rule(
    rule_id: str,
    user: Annotated[UserExtra, Depends(get_current_user)],
) -> None:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin only")
    row = await EmailRule.get(to_object_id(rule_id))
    if row is None:
        raise HTTPException(status_code=404, detail="not found")
    await row.delete()
