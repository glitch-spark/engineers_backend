"""One-off backfill: rebuild AI usage history in `ai_usage_events` from the records that predate the ledger.

Sources and how each is attributed:
  - resumeapplications: the resume step is exact (stored tokens x catalog price; a cache hit made no call).
    The rest of `estimatedCostUsd` belongs to screening and/or the cover letter (including /ask answers and
    regenerated letters, all folded into the same total). When both ran, the rest is split by the catalog's
    per-call estimates (`estimated=True`); when one ran it gets the rest exactly; when neither did, the rest
    is kept as feature "other".
  - resume_prompt_evals rows: generation (`eval_resume`) and judge (`eval_judge`) with their stored cost;
    cached rows made no call.
  - aireviewruns, analyzechatlogs, interviewchatlogs: priced from their stored tokens.
Job-posting extraction and interview analysis stored nothing, so their history can't be rebuilt.

Every event is dated at its source record's createdAt and marked source="backfill". Only records created
before --until (the deploy time of the live ledger) are used, and existing backfill events in the window are
deleted first, so the script can be rerun.

Run:
    cd engineers_backend
    .venv/Scripts/python -m scripts.backfill_ai_usage --until 2026-10-15T12:00:00Z --dry-run
    .venv/Scripts/python -m scripts.backfill_ai_usage --until 2026-10-15T12:00:00Z
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.model_catalog import TASK_TOKEN_PROFILE, spec_for_api_model, usage_cost  # noqa: E402

COVER_LETTER_CALLS = 3  # draft + critique + revise; retries make it 2-4 in practice


def _provider(model: Optional[str]) -> str:
    spec = spec_for_api_model(model)
    if spec is not None:
        return spec.provider
    name = (model or "").lower()
    return "anthropic" if name.startswith("claude") else "openai" if name else ""


def _price(model: Optional[str], tin: Any, tout: Any) -> Optional[float]:
    spec = spec_for_api_model(model)
    return usage_cost(spec, {"input": tin or 0, "output": tout or 0}) if spec is not None else None


def _per_call(model: Optional[str], task: str) -> float:
    tin, tout = TASK_TOKEN_PROFILE[task]
    return _price(model, tin, tout) or 0.0


def _event(src: dict, feature: str, model: Optional[str], cost: Optional[float], *, user=None, ref=None,
           tin: Any = 0, tout: Any = 0, calls: int = 1, estimated: bool = False) -> dict:
    at = src["createdAt"]
    return {
        "createdAt": at, "updatedAt": at, "userId": user, "feature": feature, "provider": _provider(model),
        "model": model or "", "inputTokens": int(tin or 0), "outputTokens": int(tout or 0), "reasoningTokens": 0,
        "costUsd": cost, "calls": calls, "fallbackUsed": False, "ok": True,
        "refId": str(ref) if ref is not None else None, "source": "backfill", "estimated": estimated,
    }


def _job_events(job: dict) -> list[dict]:
    out: list[dict] = []
    user, ref = job.get("userId"), job.get("_id")
    total = float(job.get("estimatedCostUsd") or 0.0)
    rmodel = job.get("resumeLlmModel")
    rin, rout = job.get("inputTokens") or 0, job.get("outputTokens") or 0
    rcost = 0.0
    if rmodel and (rin or rout):  # zero tokens = served from the resume cache, no API call
        rcost = _price(rmodel, rin, rout) or 0.0
        out.append(_event(job, "resume", rmodel, _price(rmodel, rin, rout), user=user, ref=ref, tin=rin, tout=rout))
    rest = max(total - rcost, 0.0)
    smodel = job.get("screeningLlmModel")
    cmodel = job.get("coverLetterLlmModel") if job.get("coverLetterText") else None
    if smodel and cmodel:
        ws, wc = _per_call(smodel, "screening"), COVER_LETTER_CALLS * _per_call(cmodel, "cover_letter")
        share = ws / (ws + wc) if ws + wc else 0.5
        out.append(_event(job, "screening", smodel, rest * share, user=user, ref=ref, estimated=True))
        out.append(_event(job, "cover_letter", cmodel, rest * (1 - share), user=user, ref=ref,
                          calls=COVER_LETTER_CALLS, estimated=True))
    elif smodel:
        out.append(_event(job, "screening", smodel, rest, user=user, ref=ref))
    elif cmodel:
        out.append(_event(job, "cover_letter", cmodel, rest, user=user, ref=ref, calls=COVER_LETTER_CALLS))
    elif rest > 1e-9:
        out.append(_event(job, "other", None, rest, user=user, ref=ref, estimated=True))
    return out


def _eval_events(run: dict) -> list[dict]:
    out: list[dict] = []
    user, ref = run.get("userId"), run.get("_id")
    for row in run.get("rows") or []:
        for prefix, feature in (("gen", "eval_resume"), ("judge", "eval_judge")):
            model = row.get(f"{prefix}Model")
            tin, tout = row.get(f"{prefix}InputTokens"), row.get(f"{prefix}OutputTokens")
            if not model or row.get(f"{prefix}Cached") or not (tin or tout or row.get(f"{prefix}CostUsd")):
                continue
            cost = row.get(f"{prefix}CostUsd")
            if cost is None:
                cost = _price(model, tin, tout)
            out.append(_event(run, feature, model, cost, user=user, ref=ref, tin=tin, tout=tout))
    return out


_CHAT_SOURCES = (
    ("aireviewruns", "ai_review", "ranBy", None),
    ("analyzechatlogs", "analyze_chat", "userId", None),
    ("interviewchatlogs", "interview_chat", "userId", "interviewId"),
)


# Projections keep resume HTML / inline PDFs / transcripts out of the scan.
_JOB_FIELDS = {k: 1 for k in ("createdAt", "userId", "estimatedCostUsd", "inputTokens", "outputTokens",
                              "resumeLlmModel", "screeningLlmModel", "coverLetterLlmModel", "coverLetterText")}
_EVAL_ROW_FIELDS = [f"rows.{p}{k}" for p in ("gen", "judge")
                    for k in ("Model", "InputTokens", "OutputTokens", "CostUsd", "Cached")]
_EVAL_FIELDS = {k: 1 for k in ("createdAt", "userId", *_EVAL_ROW_FIELDS)}


async def build_events(db, until: datetime, since: Optional[datetime] = None) -> list[dict]:
    window: dict = {"$lt": until}
    if since is not None:
        window["$gte"] = since
    events: list[dict] = []
    async for job in db.resumeapplications.find({"createdAt": window}, _JOB_FIELDS):
        events.extend(_job_events(job))
    async for run in db.resume_prompt_evals.find({"createdAt": window}, _EVAL_FIELDS):
        events.extend(_eval_events(run))
    for coll, feature, user_key, ref_key in _CHAT_SOURCES:
        fields = {k: 1 for k in ("createdAt", "model", "promptTokens", "completionTokens", user_key)}
        if ref_key:
            fields[ref_key] = 1
        async for doc in db[coll].find({"createdAt": window}, fields):
            model, tin, tout = doc.get("model"), doc.get("promptTokens"), doc.get("completionTokens")
            events.append(_event(doc, feature, model, _price(model, tin, tout), user=doc.get(user_key),
                                 ref=doc.get(ref_key) if ref_key else None, tin=tin, tout=tout))
    return events


def summarize(events: list[dict]) -> dict:
    by_feature: dict = defaultdict(lambda: {"events": 0, "calls": 0, "costUsd": 0.0, "estimatedCostUsd": 0.0})
    for e in events:
        f = by_feature[e["feature"]]
        f["events"] += 1
        f["calls"] += e["calls"]
        f["costUsd"] += e["costUsd"] or 0.0
        if e["estimated"]:
            f["estimatedCostUsd"] += e["costUsd"] or 0.0
    return {
        "events": len(events),
        "costUsd": sum(f["costUsd"] for f in by_feature.values()),
        "byFeature": dict(by_feature),
    }


async def backfill(db, until: datetime, since: Optional[datetime] = None, dry_run: bool = False) -> dict:
    events = await build_events(db, until, since)
    if not dry_run:
        window: dict = {"$lt": until}
        if since is not None:
            window["$gte"] = since
        await db.ai_usage_events.delete_many({"source": "backfill", "createdAt": window})
        for i in range(0, len(events), 1000):
            await db.ai_usage_events.insert_many(events[i:i + 1000])
    return summarize(events)


def _instant(raw: str) -> datetime:
    value = datetime.fromisoformat(raw)
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--until", required=True, help="ISO instant the live ledger started (records before it are used)")
    ap.add_argument("--since", help="ISO instant: only records from here on (default: all history)")
    ap.add_argument("--dry-run", action="store_true", help="print totals without writing")
    args = ap.parse_args()

    from motor.motor_asyncio import AsyncIOMotorClient

    from app.config import get_settings

    db = AsyncIOMotorClient(get_settings().MONGODB_URI).get_default_database()
    summary = await backfill(db, _instant(args.until), _instant(args.since) if args.since else None, args.dry_run)
    print(f"{'DRY RUN: ' if args.dry_run else ''}{summary['events']} events, ${summary['costUsd']:.2f}")
    for feature, f in sorted(summary["byFeature"].items(), key=lambda kv: -kv[1]["costUsd"]):
        print(f"  {feature:<16} {f['calls']:>6} calls  ${f['costUsd']:>9.2f}  (estimated ${f['estimatedCostUsd']:.2f})")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
