"""Per-call AI usage ledger: every billed LLM call writes one AiUsageEvent.

Call sites that know the user and feature pass them to `record()`. Background work that calls deeper
helpers (queue jobs, the Job Applies worker, interview analysis, eval runs) wraps them in
`usage_scope(...)` instead, and `record()` fills whatever the call site left out from the scope.
Recording never raises: a ledger failure must not fail the generation it describes."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Any, Iterator, Optional

from beanie import PydanticObjectId

from app.model_catalog import api_model_cost

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class UsageScope:
    user_id: Optional[str] = None
    feature: Optional[str] = None
    ref_id: Optional[str] = None


_scope: ContextVar[UsageScope] = ContextVar("ai_usage_scope", default=UsageScope())


def current_scope() -> UsageScope:
    return _scope.get()


@contextmanager
def usage_scope(
    *, user_id: Any = None, feature: Optional[str] = None, ref_id: Any = None
) -> Iterator[UsageScope]:
    """Name the user / feature / reference for LLM calls made inside the block. Unset fields keep the
    enclosing scope's values, so an eval runner can set the user once and the feature per step."""
    outer = _scope.get()
    changes: dict[str, Optional[str]] = {}
    if user_id is not None:
        changes["user_id"] = str(user_id)
    if feature is not None:
        changes["feature"] = feature
    if ref_id is not None:
        changes["ref_id"] = str(ref_id)
    scope = replace(outer, **changes)
    token = _scope.set(scope)
    try:
        yield scope
    finally:
        _scope.reset(token)


def _object_id(value: Any) -> Optional[PydanticObjectId]:
    if value is None or value == "":
        return None
    try:
        return PydanticObjectId(str(value))
    except Exception:  # noqa: BLE001 — a malformed id just leaves the event unattributed
        return None


def user_id_of(user: Any) -> Optional[str]:
    """The id of a UserExtra-like object (or None)."""
    uid = getattr(user, "id", None) if user is not None else None
    return str(uid) if uid is not None else None


async def record(
    *,
    provider: str,
    model: str,
    usage: Optional[dict],
    feature: Optional[str] = None,
    user_id: Any = None,
    ok: bool = True,
    fallback_used: bool = False,
    ref_id: Any = None,
) -> None:
    """Write one ledger row for a billed call. `usage` uses the router's keys: input / output / reasoning.
    The enclosing usage_scope supplies the feature, user and ref when the call site doesn't; a scope
    feature wins over the call site's generic one so evals are not counted as user resumes."""
    from app.models.ai_usage_event import AiUsageEvent  # lazy: keeps this module import-light

    usage = usage or {}
    scope = _scope.get()
    try:
        event = AiUsageEvent(
            userId=_object_id(user_id if user_id is not None else scope.user_id),
            feature=scope.feature or feature or "other",
            provider=provider or "",
            model=model or "",
            inputTokens=int(usage.get("input") or 0),
            outputTokens=int(usage.get("output") or 0),
            reasoningTokens=int(usage.get("reasoning") or 0),
            costUsd=api_model_cost(model, usage),
            fallbackUsed=fallback_used,
            ok=ok,
            refId=str(ref_id) if ref_id is not None else scope.ref_id,
        )
        await event.insert()
    except Exception as exc:  # noqa: BLE001 — never fail the caller over the ledger
        log.warning("ai usage record failed (%s %s): %s", feature or scope.feature, model, exc)


def openai_usage(response: Any) -> dict:
    """Router-style usage dict from an OpenAI Responses or Chat Completions response (or stream chunk)."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return {}
    out: dict = {}
    tin = getattr(usage, "input_tokens", None)
    if tin is None:
        tin = getattr(usage, "prompt_tokens", None)
    tout = getattr(usage, "output_tokens", None)
    if tout is None:
        tout = getattr(usage, "completion_tokens", None)
    if tin is not None:
        out["input"] = int(tin)
    if tout is not None:
        out["output"] = int(tout)
    details = getattr(usage, "output_tokens_details", None) or getattr(usage, "completion_tokens_details", None)
    reasoning = getattr(details, "reasoning_tokens", None) if details is not None else None
    if reasoning is not None:
        out["reasoning"] = int(reasoning)
    return out
