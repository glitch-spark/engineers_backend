"""Bidder routes. This module currently holds the cron fallback only."""

from typing import Annotated, Optional

from fastapi import APIRouter, Header, HTTPException, status

from app.bids.reports import run_bid_reports_tick
from app.config import get_settings

cron_router = APIRouter(prefix="/internal/bids", tags=["bids-cron"])


@cron_router.post("/tick")
async def bids_tick(
    x_cron_secret: Annotated[Optional[str], Header()] = None,
) -> dict:
    s = get_settings()
    expected = (s.SLACK_BOT_CRON_SECRET or "").strip()
    if not expected or (x_cron_secret or "").strip() != expected:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="unauthorized")
    result = await run_bid_reports_tick()
    return {"ok": True, **result}
