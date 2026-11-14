"""Confirm or unconfirm a caller round (the caller or an interview manager says they will be on the call)."""

from __future__ import annotations

from typing import Optional

from app.caller_access import can_confirm
from app.interview_caller import ENDED_STATUSES
from app.models import Interview, UserExtra

NOT_ALLOWED = "Only the caller or an interview manager can confirm."


class ConfirmError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


async def refresh_today(now=None) -> bool:  # replaced by app.interview_day.post.refresh_today in Task 8
    return False


async def confirm_round(iv_id: str, round_id: str, user: UserExtra, confirmed: bool) -> Interview:
    # The router module imports this one, so its helpers are imported at call time.
    from fastapi import HTTPException

    from app.routers import interviews as ivr

    try:
        iv = await Interview.get(ivr.to_object_id(iv_id))
    except HTTPException:
        iv = None
    if iv is None:
        raise ConfirmError(404, "not found")
    before = ivr._round_snapshots(iv)
    ivr.migrate_legacy_stage_fields(iv)
    try:
        idx = ivr._find_stage(iv, round_id)
    except HTTPException:
        raise ConfirmError(404, "stage not found")
    entry = iv.stageHistory[idx]
    if not can_confirm(iv, entry, user):
        raise ConfirmError(403, NOT_ALLOWED)
    if confirmed:
        if entry.caller is None or not entry.caller.enabled:
            raise ConfirmError(400, "Only caller rounds are confirmed")
        if ivr._status_value(entry.status) in ENDED_STATUSES:
            raise ConfirmError(400, "A canceled or rejected round can't be confirmed")
        if not entry.caller.callerId:
            raise ConfirmError(400, "Assign a caller first")
    entry.confirmed = confirmed
    iv.stageHistory[idx] = entry
    ivr._sync_top_from_tip(iv)
    await ivr._save_interview(iv)
    await ivr._sync_caller_posts(iv, before)
    await refresh_today()
    return iv
