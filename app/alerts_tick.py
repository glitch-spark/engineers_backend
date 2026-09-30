"""One-shot Railway cron entrypoint: Slack digest, then in-app alerts, then exit.

Start command: python -m app.alerts_tick
Must not start uvicorn — Railway skips the next cron if this process stays Active.

Run Slack first so a slow inbox pass cannot miss the 5-minute digest window.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from app.config import get_settings
from app.db import close_db, init_db

log = logging.getLogger("alerts_tick")


async def run_cron_ticks() -> int:
    """Run Slack digest then in-app alerts. Returns 0 if both succeed."""
    code = 0
    try:
        from app.slack_bot import run_alert_tick

        slack = await run_alert_tick()
        log.info("slack digest tick %s", slack)
    except Exception:
        log.exception("slack digest tick failed")
        code = 1
    try:
        from app.alerts_service import run_alerts_tick

        alerts = await run_alerts_tick()
        log.info("alerts tick %s", alerts)
    except Exception:
        log.exception("alerts tick failed")
        code = 1
    return code


def _setup_logging() -> None:
    """INFO/WARNING to stdout, ERROR+ to stderr — Railway marks every stderr line as an error."""
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    out = logging.StreamHandler(sys.stdout)
    out.addFilter(lambda record: record.levelno < logging.ERROR)
    err = logging.StreamHandler(sys.stderr)
    err.setLevel(logging.ERROR)
    for handler in (out, err):
        handler.setFormatter(fmt)
    logging.basicConfig(level=logging.INFO, handlers=[out, err])


async def _run() -> int:
    _setup_logging()
    settings = get_settings()
    os.environ["MONGODB_URI"] = settings.MONGODB_URI
    await init_db()
    try:
        return await run_cron_ticks()
    finally:
        await close_db()


def main() -> None:
    sys.exit(asyncio.run(_run()))


if __name__ == "__main__":
    main()
