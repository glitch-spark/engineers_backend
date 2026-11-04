import asyncio
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import get_settings
from app.db import init_db
from app import redis_cache, resume_queue
from app.job_apply import worker as job_apply_worker
from app.routers import (
    accounts,
    ai_review,
    ai_usage,
    auth as auth_router,
    bidder_ext,
    bidders,
    bids,
    callers,
    dashboard,
    global_prompt,
    interviews,
    job_applies,
    metrics,
    profile,
    prompt_eval,
    reports,
    resume,
    skills,
    slack_bot,
    transactions,
    users,
    weekly_plans,
    daily_plans,
    alerts,
)


log = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    # Bridge .env loading into db.py (which still reads os.environ directly).
    os.environ["MONGODB_URI"] = settings.MONGODB_URI
    await init_db()
    try:
        from app.bids.folders import backfill_folders

        n = await backfill_folders()
        if n:
            log.info("Assigned folders to %d bidders", n)
    except Exception:
        log.exception("Bidder folder backfill failed")
    if resume_queue.queue_enabled():  # the deployed backend: local runs share its database and must not record it
        try:
            from app.dashboard_stats import app_tz
            from app.profile_metrics import record_launch

            await record_launch(datetime.now(app_tz()).date())
        except Exception:
            log.exception("Recording the profile metrics launch day failed")
    sweep = None
    if job_apply_worker.worker_enabled():
        # Restarts Job Applies runs whose worker died (redeploy/crash); see app/job_apply/worker.py.
        sweep = asyncio.create_task(job_apply_worker.sweep_forever())
    queue = asyncio.create_task(resume_queue.queue_forever()) if resume_queue.queue_enabled() else None
    yield
    if sweep:
        sweep.cancel()
    if queue:
        queue.cancel()
    await redis_cache.close()


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="engineers_backend", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[settings.FRONTEND_ORIGIN],
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["Authorization", "Content-Type"],
        expose_headers=["Content-Disposition", "X-Folder"],
    )

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException):
        detail = exc.detail
        body = {"error": detail} if isinstance(detail, str) else detail
        return JSONResponse(
            status_code=exc.status_code,
            content=body,
            headers=getattr(exc, "headers", None) or {},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        return JSONResponse(status_code=400, content={"error": "Invalid request"})

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    app.include_router(auth_router.router)
    app.include_router(profile.router)
    app.include_router(users.router)
    app.include_router(accounts.router)
    app.include_router(transactions.router)
    app.include_router(alerts.router)
    app.include_router(alerts.cron_router)
    app.include_router(weekly_plans.router)
    app.include_router(daily_plans.router)
    app.include_router(interviews.router)
    app.include_router(callers.router)
    app.include_router(skills.router)
    app.include_router(global_prompt.router)
    app.include_router(ai_review.router)
    app.include_router(ai_usage.router)
    app.include_router(metrics.router)
    app.include_router(dashboard.router)
    app.include_router(reports.router)
    app.include_router(resume.router)
    app.include_router(prompt_eval.router)
    app.include_router(job_applies.router)
    app.include_router(slack_bot.router)
    app.include_router(slack_bot.cron_router)
    app.include_router(bidders.router)
    app.include_router(bidder_ext.router)
    app.include_router(bidders.cron_router)
    app.include_router(bids.router)

    return app


app = create_app()
