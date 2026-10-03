"""Object keys for the extension's screenshots: <folder>/<D>/<profile>/<job>/step<n>_<trigger>_<D>_<HHMMSS>.png."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Optional

from app.bids.folders import profile_segment, slug

MAX_AGE = timedelta(hours=24)


def job_slug(job: str) -> str:
    s = re.sub(r"[^a-z0-9._-]+", "-", (job or "").lower()).strip("-_.")
    s = s[:80].strip("-_.")
    return s or "job"


def build_upload_key(
    folder: str, profile_name: Optional[str], job: str, step: int, trigger: str,
    captured_at: datetime, now: datetime,
) -> str:
    """Date and time come from captured_at in its own UTC offset (the bidder's local clock)."""
    if abs(captured_at - now) > MAX_AGE:
        raise ValueError("capturedAt is not current")
    day = captured_at.strftime("%Y-%m-%d")
    stamp = captured_at.strftime("%H%M%S")
    return f"{folder}/{day}/{profile_segment(profile_name)}/{job_slug(job)}/step{step}_{trigger}_{day}_{stamp}.png"
