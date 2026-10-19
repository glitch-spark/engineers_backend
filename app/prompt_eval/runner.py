"""Runs one eval: generate a resume per JD in parallel (bounded), judge each, persist as it goes.

A row failure (generation or judge) marks that row failed and keeps whatever it produced; the run
still finishes `done`. Only an error outside the rows (profile gone, DB write failed) fails the run.
There is no lease or heartbeat: a run still `running` after STALE_AFTER is reported failed by the
API (`mark_stale_runs`), which covers a server restart mid-run.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Optional

from beanie import PydanticObjectId
from beanie.odm.utils.encoder import Encoder

from app import ai_usage
from app.config import get_settings
from app.models import Account, EvalJobDescription, ResumePromptEval, UserExtra
from app.job_apply.scoring import Idf
from app.job_apply.schema import JobExtraction
from app.models.prompt_eval import EVAL_ROLES, EvalRow, RoleScore
from app.prompt_eval import rules_judge
from app.prompt_eval.judge import judge_resume
from app.resume_service import generate_resume_html, plain_text_from_html

log = logging.getLogger(__name__)

STALE_AFTER = timedelta(minutes=20)
STALE_ERROR = "Run timed out (the server may have restarted while it was running)"

_tasks: set[asyncio.Task] = set()
_sem: Optional[asyncio.Semaphore] = None


# ---------- pure helpers ----------


def is_stale(status: str, started_at: Optional[datetime], now: Optional[datetime] = None) -> bool:
    if status != "running" or started_at is None:
        return False
    return (now or datetime.utcnow()) - started_at > STALE_AFTER


def _mean(values: list[float]) -> Optional[float]:
    return round(sum(values) / len(values), 1) if values else None


def aggregate(rows: list[EvalRow]) -> dict:
    """Run-level numbers from the rows: averages over scored rows, per-role averages, cost, cache hits."""
    scored = [r for r in rows if r.status == "done" and r.verdict is not None]
    role_scores: dict[str, RoleScore] = {}
    for role in EVAL_ROLES:
        in_role = [r for r in scored if r.role == role]
        if in_role:
            role_scores[role] = RoleScore(
                match=_mean([float(r.verdict.matchScore) for r in in_role]),
                believability=_mean([float(r.verdict.believabilityScore) for r in in_role]),
                n=len(in_role),
            )
    cost = sum((r.genCostUsd or 0.0) + (r.judgeCostUsd or 0.0) for r in rows)
    return {
        "avgMatch": _mean([float(r.verdict.matchScore) for r in scored]),
        "avgBelievability": _mean([float(r.verdict.believabilityScore) for r in scored]),
        "roleScores": role_scores,
        "totalCostUsd": round(cost, 6),
        "cachedRows": sum(1 for r in rows if r.genCached),
        "cachedJudgeRows": sum(1 for r in rows if r.judgeCached),
        "counts": {
            "total": len(rows),
            "done": sum(1 for r in rows if r.status == "done"),
            "failed": sum(1 for r in rows if r.status == "failed"),
        },
    }


def _slots() -> asyncio.Semaphore:
    global _sem
    if _sem is None:
        _sem = asyncio.Semaphore(max(1, get_settings().PROMPT_EVAL_CONCURRENCY))
    return _sem


# ---------- persistence (module attributes so tests can swap them) ----------


async def _persist_row(run_id: PydanticObjectId, index: int, row: EvalRow) -> None:
    # No upsert: a run deleted while running stays deleted.
    await ResumePromptEval.get_motor_collection().update_one(
        {"_id": run_id},
        {"$set": {f"rows.{index}": Encoder(to_db=True).encode(row.model_dump()), "updatedAt": datetime.utcnow()}},
    )


async def _finish(run_id: PydanticObjectId, rows: list[EvalRow], started: float, *, error: Optional[str] = None) -> None:
    summary = aggregate(rows)
    now = datetime.utcnow()
    fields = {
        **summary,
        "roleScores": {k: v.model_dump() for k, v in summary["roleScores"].items()},
        "status": "failed" if error else "done",
        "error": error,
        "finishedAt": now,
        "durationMs": int((time.monotonic() - started) * 1000),
        "updatedAt": now,
    }
    await ResumePromptEval.get_motor_collection().update_one({"_id": run_id}, {"$set": fields})


async def mark_stale_runs(runs: list[ResumePromptEval]) -> None:
    """Flip runs that can no longer be running to failed (in the DB and on the given objects)."""
    now = datetime.utcnow()
    stale = [r for r in runs if is_stale(r.status, r.startedAt, now)]
    if not stale:
        return
    await ResumePromptEval.get_motor_collection().update_many(
        {"_id": {"$in": [r.id for r in stale]}, "status": "running"},
        {"$set": {"status": "failed", "error": STALE_ERROR, "finishedAt": now, "updatedAt": now}},
    )
    for r in stale:
        r.status = "failed"
        r.error = STALE_ERROR
        r.finishedAt = now


# ---------- the work ----------


@dataclass
class RulesContext:
    """What the rules judge shares across a run's rows: each job description's reading, its cost per row,
    whether it was reused, and the run's term rarity."""

    idf: Idf
    run_date: date
    readings: dict[str, JobExtraction] = field(default_factory=dict)
    cost_per_row: dict[str, Optional[float]] = field(default_factory=dict)
    reused: dict[str, bool] = field(default_factory=dict)


async def _rules_context(run: ResumePromptEval, jds: list, owner: Optional[UserExtra]) -> RulesContext:
    run_date = (run.startedAt or datetime.utcnow()).date()
    ctx = RulesContext(idf=rules_judge.build_run_idf([j.text for j in jds]), run_date=run_date)
    uses: dict[str, int] = {}
    for r in run.rows:
        uses[str(r.jdId)] = uses.get(str(r.jdId), 0) + 1
    for jd in jds:
        key = str(jd.id)
        ex, cost, reused = await rules_judge.jd_reading(jd, owner, run_date=run_date)
        ctx.readings[key] = ex
        ctx.reused[key] = reused
        ctx.cost_per_row[key] = None if cost is None else cost / max(1, uses.get(key, 1))
    return ctx


async def _run_row(
    run: ResumePromptEval,
    index: int,
    row: EvalRow,
    account: Account,
    owner: Optional[UserExtra],
    jd_text: Optional[str],
    rules: Optional[RulesContext] = None,
) -> EvalRow:
    started = time.monotonic()
    try:
        if not (jd_text or "").strip():
            raise ValueError("job description was deleted")
        row.status = "generating"
        await _persist_row(run.id, index, row)

        with ai_usage.usage_scope(user_id=run.userId, feature="eval_resume", ref_id=run.id):
            html, gen = await generate_resume_html(
                account, jd_text, prompt_body=run.promptBody or None, owner=owner, model_id=run.resumeModelId,
            )
        row.genModelId = gen.model_id or None
        row.genProvider = gen.provider
        row.genModel = gen.model
        row.genFallbackUsed = bool(gen.fallback_used)
        row.genCached = bool(gen.cached)
        row.genInputTokens = gen.usage.get("input")
        row.genOutputTokens = gen.usage.get("output")
        row.genCostUsd = gen.cost_usd
        row.resumeHtml = html
        row.resumeText = plain_text_from_html(html)
        row.status = "judging"
        await _persist_row(run.id, index, row)

        if rules is not None:
            key = str(row.jdId)
            extraction = rules.readings.get(key)
            if extraction is None:
                raise ValueError("job description could not be read for the rules judge")
            row.verdict = await asyncio.to_thread(
                rules_judge.judge_rules, resume_text=row.resumeText, jd_text=jd_text, extraction=extraction,
                idf=rules.idf, run_date=rules.run_date,
            )
            row.judgeModel = "rules"
            row.judgeCached = rules.reused.get(key, False)
            row.judgeCostUsd = rules.cost_per_row.get(key)
            row.status = "done"
            row.durationMs = int((time.monotonic() - started) * 1000)
            await _persist_row(run.id, index, row)
            return row

        with ai_usage.usage_scope(user_id=run.userId, feature="eval_judge", ref_id=run.id):
            verdict, jr = await judge_resume(
                resume_text=row.resumeText, job_description=jd_text, role=row.role, user=owner,
                model_id=run.judgeModelId,
            )
        row.judgeModelId = jr.model_id or None
        row.judgeProvider = jr.provider
        row.judgeModel = jr.model
        row.judgeFallbackUsed = bool(jr.fallback_used)
        row.judgeCached = bool(jr.cached)
        row.judgeInputTokens = jr.usage.get("input")
        row.judgeOutputTokens = jr.usage.get("output")
        row.judgeCostUsd = jr.cost_usd
        row.verdict = verdict
        row.status = "done"
    except Exception as exc:  # noqa: BLE001 — one JD failing must not take the run down
        log.exception("prompt eval %s row %d (%s) failed", run.id, index, row.jdTitle)
        row.status = "failed"
        row.error = (str(exc) or exc.__class__.__name__)[:500]
    row.durationMs = int((time.monotonic() - started) * 1000)
    await _persist_row(run.id, index, row)
    return row


async def run_eval(run_id: PydanticObjectId) -> None:
    started = time.monotonic()
    run = await ResumePromptEval.get(run_id)
    if run is None or run.status != "running":
        return
    rows = list(run.rows)
    try:
        account = await Account.get(run.accountId)
        if account is None:
            await _finish(run_id, rows, started, error="profile no longer exists")
            return
        owner = await UserExtra.get(run.userId)
        jds = await EvalJobDescription.find({"_id": {"$in": [r.jdId for r in rows]}}).to_list()
        text_by_id = {str(j.id): j.text for j in jds}
        rules = await _rules_context(run, jds, owner) if run.judgeMode == "rules" else None

        async def one(index: int, row: EvalRow) -> EvalRow:
            async with _slots():
                return await _run_row(run, index, row, account, owner, text_by_id.get(str(row.jdId)), rules)

        results = await asyncio.gather(*(one(i, r) for i, r in enumerate(rows)), return_exceptions=True)
        for index, result in enumerate(results):
            if isinstance(result, BaseException):  # only a bug in _run_row's own error path gets here
                rows[index].status = "failed"
                rows[index].error = (str(result) or result.__class__.__name__)[:500]
                await _persist_row(run_id, index, rows[index])
            else:
                rows[index] = result
        await _finish(run_id, rows, started)
    except Exception as exc:  # noqa: BLE001
        log.exception("prompt eval %s failed", run_id)
        await _finish(run_id, rows, started, error=(str(exc) or exc.__class__.__name__)[:500])


def start_run(run_id: PydanticObjectId) -> None:
    task = asyncio.create_task(run_eval(run_id))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
