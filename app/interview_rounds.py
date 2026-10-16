"""Interview round counting for dashboard / leaderboard metrics.

Pure counting + normalization logic lives here so it can be unit-tested
directly and reused outside `app.routers.metrics` (e.g. the dashboard
router). Behaviour matches the Interviews board's own stage badges.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from app.models import Interview

# Rounds excluded from interview KPIs / leaderboard (matches Interviews board).
_ROUND_COUNT_EXCLUDED = frozenset({
    "ai_interview",
    "home_assessment",
})

# Board-style breakdown labels (order preserved for display).
BREAKDOWN_ORDER: tuple[tuple[str, str], ...] = (
    ("intro", "Intro"),
    ("tech", "Tech"),
    ("hiring", "Hiring"),
    ("panel", "Panel"),
    ("final", "Final"),
)


def _breakdown_group(stage: str) -> Optional[str]:
    if stage in _ROUND_COUNT_EXCLUDED:
        return None
    if stage == "intro":
        return "intro"
    if stage in {
        "tech", "tech1", "tech2",
        "tech_round_1", "tech_round_2",
        "live_coding", "system_design",
    }:
        return "tech"
    if stage in {"cultural", "hiring_manager"}:
        return "hiring"
    if stage == "panel":
        return "panel"
    if stage in {"final", "offer"}:
        return "final"
    return None


def empty_breakdown() -> dict[str, int]:
    return {key: 0 for key, _ in BREAKDOWN_ORDER}


def _norm_stage(stage: Any) -> Optional[str]:
    if stage is None:
        return None
    if hasattr(stage, "value"):
        stage = stage.value
    s = str(stage).strip()
    if s.startswith("InterviewStage."):
        s = s.split(".", 1)[1]
    if s in ("tech", "tech1"):
        return "tech_round_1"
    if s == "tech2":
        return "tech_round_2"
    if s == "hiring_manager":
        return "cultural"
    return s or None


def _norm_status(status: Any) -> str:
    if status is None:
        return ""
    if hasattr(status, "value"):
        status = status.value
    return str(status).strip()


def _as_aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _dt_in_range(dt: Optional[datetime], start: datetime, end: datetime) -> bool:
    d = _as_aware(dt)
    if d is None:
        return False
    return _as_aware(start) <= d <= _as_aware(end)  # type: ignore[operator]


def _dated_rounds(iv: Interview) -> list[tuple[str, Optional[datetime], str, Any]]:
    """Trail of (stage, scheduledAt, status, caller) — history keeps its own dates,
    statuses and callers; the tip falls back to the interview-level values."""
    trail: list[tuple[str, Optional[datetime], str, Any]] = []

    def push(stage_raw: Any, dt: Optional[datetime], status_raw: Any, caller: Any) -> None:
        stage = _norm_stage(stage_raw)
        if not stage:
            return
        status = _norm_status(status_raw)
        if trail and trail[-1][0] == stage:
            _, prev_dt, prev_status, prev_caller = trail[-1]
            trail[-1] = (
                stage,
                dt if dt is not None else prev_dt,
                status or prev_status,
                caller if caller is not None else prev_caller,
            )
            return
        trail.append((stage, dt, status, caller))

    for h in (iv.stageHistory or []):
        push(
            getattr(h, "stage", None),
            getattr(h, "scheduledAt", None),
            getattr(h, "status", None),
            getattr(h, "caller", None),
        )
    push(iv.stage, iv.scheduledAt, iv.status, getattr(iv, "caller", None))
    return trail


def count_interview_rounds(
    iv: Interview, start: datetime, end: datetime,
) -> tuple[int, int, dict[str, int]]:
    """Return (countable_rounds, canceled, breakdown_counts) for rounds dated in range.

    A round with status canceled counts toward `canceled`, never as held.
    Countable rounds exclude AI Interview and Home Assessment.
    """
    breakdown = empty_breakdown()
    n = canceled = 0
    for s, dt, round_status, _caller in _dated_rounds(iv):
        if not _dt_in_range(dt, start, end):
            continue
        if round_status == "canceled":
            canceled += 1
            continue
        if s in _ROUND_COUNT_EXCLUDED:
            continue
        group = _breakdown_group(s)
        if not group:
            continue
        breakdown[group] = breakdown.get(group, 0) + 1
        n += 1
    return n, canceled, breakdown


async def load_interviews_for_window(user_ids: list, start: datetime, end: datetime) -> list[Interview]:
    if not user_ids:
        return []
    return await Interview.find(
        {
            "createdBy": {"$in": user_ids},
            "$or": [
                {"scheduledAt": {"$gte": start, "$lte": end}},
                {"stageHistory.scheduledAt": {"$gte": start, "$lte": end}},
            ],
        }
    ).to_list()


def split_rounds_by_day(iv: Interview, start: datetime, end: datetime, tz) -> list[tuple]:
    """(local date, breakdown group, is_caller, stage) for each round held in the window.

    Same rules as `count_interview_rounds` (canceled rounds, AI interviews and
    home assessments don't count). A round is a caller round when it has an
    enabled caller; otherwise the owner took it (self).
    """
    out = []
    for stage, dt, round_status, caller in _dated_rounds(iv):
        if not _dt_in_range(dt, start, end) or round_status == "canceled" or stage in _ROUND_COUNT_EXCLUDED:
            continue
        group = _breakdown_group(stage)
        if not group:
            continue
        day = _as_aware(dt).astimezone(tz).date()
        out.append((day, group, bool(caller is not None and getattr(caller, "enabled", False)), stage))
    return out
