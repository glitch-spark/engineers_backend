"""One-shot Railway cron entrypoint: run the in-app alerts tick and exit.

Start command: python -m app.alerts_tick
Must not start uvicorn — Railway skips the next cron if this process stays Active.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

from app.config import get_settings
from app.db import close_db, init_db

log = logging.getLogger("alerts_tick")


async def _run() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    settings = get_settings()
    os.environ["MONGODB_URI"] = settings.MONGODB_URI
    await init_db()
    try:
        from app.alerts_service import run_alerts_tick

        result = await run_alerts_tick()
        log.info("alerts tick %s", result)
        return 0
    except Exception:
        log.exception("alerts tick failed")
        return 1
    finally:
        await close_db()


def main() -> None:
    code = asyncio.run(_run())
    sys.exit(code)


if __name__ == "__main__":
    main()
