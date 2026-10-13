"""Optional Redis cache for prompt-eval judge verdicts. Generated resumes are not cached here: they
use only the 7-day Mongo `ResumeLLMCache` (app/resume_service.py).

Keyed by a content hash of everything that determines the output (model lane + full prompt), with a
long TTL (`LLM_CACHE_TTL_SECONDS`, default 30 days), so identical work is never paid for twice.

Redis is optional, like Slack: no `REDIS_URL` means every call is a miss / no-op. Any Redis error is
logged and treated as a miss, and after a connection error Redis is skipped for `BACKOFF_SECONDS` so a
dead instance does not add a timeout to every call.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Any, Optional

from app.config import get_settings

log = logging.getLogger(__name__)

KEY_PREFIX = "llm"
BACKOFF_SECONDS = 60
SOCKET_TIMEOUT = 3

_client = None  # redis.asyncio.Redis, built lazily
_client_url: Optional[str] = None
_disabled_until: float = 0.0


def enabled() -> bool:
    return bool((get_settings().REDIS_URL or "").strip())


def cache_key(namespace: str, *parts: str) -> str:
    digest = hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()
    return f"{KEY_PREFIX}:{namespace}:{digest}"


def _get_client():
    """The shared async client, rebuilt if the URL changed (tests / reconfiguration)."""
    global _client, _client_url
    url = (get_settings().REDIS_URL or "").strip()
    if not url:
        return None
    if _client is None or _client_url != url:
        import redis.asyncio as redis_asyncio

        _client = redis_asyncio.from_url(
            url,
            decode_responses=True,
            socket_timeout=SOCKET_TIMEOUT,
            socket_connect_timeout=SOCKET_TIMEOUT,
        )
        _client_url = url
    return _client


def _available() -> bool:
    return enabled() and time.monotonic() >= _disabled_until


def _back_off(exc: Exception) -> None:
    global _disabled_until
    _disabled_until = time.monotonic() + BACKOFF_SECONDS
    log.warning("redis unavailable, skipping cache for %ss: %s", BACKOFF_SECONDS, exc)


async def get_json(key: str) -> Optional[dict[str, Any]]:
    """The stored object for `key`, or None on a miss, a corrupt value, or any Redis problem."""
    if not _available():
        return None
    try:
        raw = await _get_client().get(key)
    except Exception as exc:  # noqa: BLE001 — a cache must never break the caller
        _back_off(exc)
        return None
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        log.warning("redis value at %s is not JSON; ignoring", key)
        return None
    return value if isinstance(value, dict) else None


async def set_json(key: str, value: dict[str, Any], ttl: Optional[int] = None) -> bool:
    """Store `value` under `key` with a TTL (default `LLM_CACHE_TTL_SECONDS`). True when written."""
    if not _available():
        return False
    seconds = ttl if ttl is not None else get_settings().LLM_CACHE_TTL_SECONDS
    try:
        await _get_client().set(key, json.dumps(value), ex=max(1, int(seconds)))
        return True
    except Exception as exc:  # noqa: BLE001
        _back_off(exc)
        return False


async def delete(key: str) -> None:
    if not _available():
        return
    try:
        await _get_client().delete(key)
    except Exception as exc:  # noqa: BLE001
        _back_off(exc)


async def close() -> None:
    global _client, _client_url
    client, _client, _client_url = _client, None, None
    if client is not None:
        try:
            await client.aclose()
        except Exception as exc:  # noqa: BLE001
            log.debug("redis close: %s", exc)
