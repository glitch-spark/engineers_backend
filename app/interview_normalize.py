"""Interview stage/status normalisation shared by the model, router and migration.

Rejected and canceled used to be encoded as a trailing ``rejected`` stage entry
(``rejected`` + ``scheduled`` meant canceled). They are now round statuses.
``resolve_interview`` turns a raw interview document into the new shape; the
model runs it on read (so unmigrated documents load), and
``scripts/migrate_interview_statuses.py`` runs it to persist the result.

Must not import app.models or app.routers — the model imports this module.
"""

from __future__ import annotations

import copy
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Optional

STATUS_VALUES: tuple[str, ...] = ("scheduled", "completed", "passed", "rejected", "canceled")
STAGE_VALUES: tuple[str, ...] = (
    "intro",
    "tech_round_1",
    "tech_round_2",
    "live_coding",
    "system_design",
    "home_assessment",
    "cultural",
    "panel",
    "final",
    "ai_interview",
)

_LEGACY_STATUS = {"failed": "rejected", "no_show": "canceled", "rescheduled": "scheduled"}
_LEGACY_STAGE = {
    "tech": "tech_round_1",
    "tech1": "tech_round_1",
    "tech2": "tech_round_2",
    "hiring_manager": "cultural",
    "offer": "final",
    "others": "intro",
}


def _plain(value: Any, prefix: str) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, Enum):
        value = value.value
    s = str(value).strip()
    if s.startswith(prefix):
        s = s[len(prefix):]
    return s or None


def normalize_status(value: Any) -> Optional[str]:
    """Canonical status string; legacy values mapped, unknown values returned as-is."""
    s = _plain(value, "InterviewStatus.")
    return _LEGACY_STATUS.get(s, s) if s else None


def interview_status_value(value: Any) -> Optional[str]:
    """Plain status string for readers (feeds, pipeline, Slack) — never str(enum)."""
    return normalize_status(value)


def normalize_stage(value: Any) -> Optional[str]:
    """Canonical stage string; legacy values mapped. ``rejected`` is passed through."""
    s = _plain(value, "InterviewStage.")
    return _LEGACY_STAGE.get(s, s) if s else None


def translate_legacy_write(
    stage: Optional[str], status: Optional[str],
) -> tuple[Optional[str], Optional[str]]:
    """Map a client's (stage, status) pair to what the router should apply.

    A legacy ``stage="rejected"`` never becomes a stage: it returns stage None
    (leave stage and history alone) and sets the current round's status —
    canceled when sent with ``scheduled`` (the old Canceled column), else
    rejected. Empty strings pass through (the router treats "" as clear).
    """
    if normalize_stage(stage) == "rejected":
        return None, _rejected_status(normalize_status(status))
    out_stage = "" if stage == "" else normalize_stage(stage)
    out_status = "" if status == "" else normalize_status(status)
    return out_stage, out_status


@dataclass
class Resolution:
    doc: dict
    question_remap: dict[str, str] = field(default_factory=dict)
    rules: Counter = field(default_factory=Counter)


def _merge_text(existing: Optional[str], extra: Optional[str]) -> str:
    a, b = existing or "", extra or ""
    if not b.strip():
        return a
    if not a.strip():
        return b
    return f"{a}\n\n---\n{b}"


def _utc_naive(dt: Any) -> Optional[datetime]:
    if not isinstance(dt, datetime):
        return None
    if dt.tzinfo is not None:
        return dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt


def _rejected_status(status: Optional[str]) -> str:
    return "canceled" if status == "scheduled" else "rejected"


def resolve_interview(raw: dict, *, now: datetime) -> Resolution:
    """Return ``raw`` converted to the status-based shape (input is not mutated)."""
    doc = copy.deepcopy(raw)
    res = Resolution(doc=doc)
    rules = res.rules

    def mapped(container: dict, key: str, fn) -> None:
        old = container.get(key)
        new = fn(old)
        if new != old and not (old == "" and new is None):
            container[key] = new
            rules["legacy_value_mapped"] += 1

    history: list[dict] = doc.get("stageHistory") or []
    for entry in history:
        mapped(entry, "stage", normalize_stage)
        mapped(entry, "status", normalize_status)
    mapped(doc, "stage", normalize_stage)
    mapped(doc, "status", normalize_status)

    if not history:
        if doc.get("stage") == "rejected":
            doc["stage"] = "intro"
            doc["status"] = _rejected_status(doc.get("status"))
            rules["rejected_only_to_intro"] += 1
        return res

    while history and history[-1].get("stage") == "rejected":
        tip = history[-1]
        new_status = _rejected_status(tip.get("status") or doc.get("status"))
        if len(history) == 1:
            tip["stage"] = "intro"
            tip["status"] = new_status
            rules["rejected_only_to_intro"] += 1
            break
        history.pop()
        prev = history[-1]
        prev["status"] = new_status
        prev["note"] = _merge_text(prev.get("note"), tip.get("note"))
        prev["transcript"] = _merge_text(prev.get("transcript"), tip.get("transcript"))
        if tip.get("id") and prev.get("id"):
            res.question_remap[tip["id"]] = prev["id"]
        rules["rejected_to_canceled" if new_status == "canceled" else "rejected_to_rejected"] += 1

    for entry in history[:-1]:
        if entry.get("status") is None:
            entry["status"] = "passed"
            rules["earlier_none_to_passed"] += 1

    tip = history[-1]
    if tip.get("status") is None:
        when = _utc_naive(tip.get("scheduledAt")) or _utc_naive(doc.get("scheduledAt"))
        if when is not None and when > _utc_naive(now):
            tip["status"] = "scheduled"
            rules["latest_none_to_scheduled"] += 1
        else:
            tip["status"] = "completed"
            rules["latest_none_to_completed"] += 1

    before = (doc.get("stage"), doc.get("status"), doc.get("scheduledAt"))
    doc["stage"] = tip.get("stage")
    doc["status"] = tip.get("status")
    tip_when = tip.get("scheduledAt")
    cur, new = _utc_naive(doc.get("scheduledAt")), _utc_naive(tip_when)
    if new is not None and (cur is None or cur.date() != new.date()):
        doc["scheduledAt"] = tip_when
    if (doc.get("stage"), doc.get("status"), doc.get("scheduledAt")) != before:
        rules["top_resynced"] += 1
    return res
