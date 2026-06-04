"""Rule-engine matcher + template-hash cache helpers.

This module is the cheap deterministic side of email classification.
For every incoming message we:
  1. compute a template hash and look it up in EmailClassificationCache
  2. if no cache hit, walk EmailRule rows and look for a sender+phrase match
  3. if no rule match, the caller falls back to the LLM classifier

Two intentional design decisions:

  * Rules + cache are DB-collections (Beanie), not Python files. That
    means a user correction in the Needs-Review tray can write a new
    rule with the same code path the seed rules use — there is no
    "ATS library" vs "learned rules" split to keep in sync.

  * Matching is plain substring (case-insensitive), not regex. Strings
    are admin-editable + cheap, and we get zero ReDoS surface. If a
    regex-style rule is ever needed we'll add a separate field rather
    than reinterpret the existing one.
"""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.models import EmailClassificationCache, EmailRule

log = logging.getLogger(__name__)


# Cache TTL — entries older than this are ignored on read. We do NOT
# delete; high-hitCount entries can stay valuable far past their first
# observation. Read-time check keeps things simple.
CACHE_TTL_DAYS = 30


# ---------------------------------------------------------------------------
# Template hash
# ---------------------------------------------------------------------------


_NORMALIZE_RX = re.compile(r"\s+")


def _normalize(text: str) -> str:
    """Lowercase + collapse whitespace. Anchors the hash to the template
    skeleton instead of per-recipient personalization."""
    return _NORMALIZE_RX.sub(" ", (text or "").lower()).strip()


def sender_domain(addr: str) -> str:
    """Returns just the @domain.tld piece (with leading @) so the cache
    groups by sender platform, not per-employee mail address."""
    addr = (addr or "").lower().strip()
    if "@" in addr:
        return "@" + addr.rsplit("@", 1)[-1]
    return addr


def template_hash(*, from_addr: str, subject: str, body_or_snippet: str) -> str:
    """Stable hash for cache lookup. Uses the sender DOMAIN (not full
    address) + normalized subject + first 200 chars of body. The body
    prefix is enough to identify the boilerplate skeleton; per-recipient
    personalization tends to live later in the message."""
    h = hashlib.sha256()
    h.update(sender_domain(from_addr).encode())
    h.update(b"\x1f")
    h.update(_normalize(subject)[:200].encode())
    h.update(b"\x1f")
    h.update(_normalize(body_or_snippet)[:200].encode())
    return h.hexdigest()


# ---------------------------------------------------------------------------
# Rule matching
# ---------------------------------------------------------------------------


def _match_rule(rule: EmailRule, *, from_addr: str, subject: str, body: str) -> bool:
    sp = (rule.senderPattern or "").lower().strip()
    pp = (rule.phrasePattern or "").lower().strip()
    if not sp or not pp:
        return False
    if sp not in (from_addr or "").lower():
        return False
    haystack = ((subject or "") + "\n" + (body or "")).lower()
    return pp in haystack


async def find_matching_rule(
    *, from_addr: str, subject: str, body: str
) -> Optional[EmailRule]:
    """Returns the first enabled rule whose sender + phrase patterns
    both match. Bumps hitCount/lastHitAt on match so admins can spot
    high-traffic rules.

    We pull all enabled rules at once — the table will stay small
    (hundreds at most) and walking them in Python is simpler than
    expressing the AND-substring-match as a Mongo query."""
    cur = EmailRule.find(EmailRule.enabled == True)  # noqa: E712
    async for rule in cur:
        if _match_rule(rule, from_addr=from_addr, subject=subject, body=body):
            rule.hitCount += 1
            rule.lastHitAt = datetime.now(timezone.utc)
            try:
                await rule.save()
            except Exception as exc:  # noqa: BLE001
                log.debug("rule hit-counter update failed: %s", exc)
            return rule
    return None


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


async def cache_lookup(tpl_hash: str) -> Optional[EmailClassificationCache]:
    row = await EmailClassificationCache.find_one(
        EmailClassificationCache.templateHash == tpl_hash
    )
    if row is None:
        return None
    # Soft-expire stale entries — but keep the row so re-classifying it
    # refreshes rather than racing on insert.
    if row.createdAt and row.createdAt < datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=CACHE_TTL_DAYS):
        return None
    row.hitCount += 1
    row.lastHitAt = datetime.now(timezone.utc)
    try:
        await row.save()
    except Exception as exc:  # noqa: BLE001
        log.debug("cache hit-counter update failed: %s", exc)
    return row


async def cache_store(
    *,
    tpl_hash: str,
    label: str,
    confidence: float,
    stage_round: Optional[str],
    company_hint: Optional[str],
    evidence_quote: Optional[str],
    sender_domain_str: str,
    subject_preview: str,
) -> None:
    """Idempotent upsert. Duplicate hash (rare race) is fine — the
    unique index just rejects the second insert and we no-op."""
    existing = await EmailClassificationCache.find_one(
        EmailClassificationCache.templateHash == tpl_hash
    )
    if existing is not None:
        existing.label = label
        existing.confidence = confidence
        existing.stageRound = stage_round
        existing.companyHint = company_hint
        existing.evidenceQuote = evidence_quote
        await existing.save()
        return
    try:
        await EmailClassificationCache(
            templateHash=tpl_hash,
            label=label,
            confidence=confidence,
            stageRound=stage_round,
            companyHint=company_hint,
            evidenceQuote=evidence_quote,
            senderDomain=sender_domain_str,
            subjectPreview=subject_preview[:120],
        ).insert()
    except Exception as exc:  # noqa: BLE001
        log.debug("cache insert raced: %s", exc)
