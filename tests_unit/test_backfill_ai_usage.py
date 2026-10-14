"""scripts/backfill_ai_usage.py: rebuild ledger history from jobs, evals and chat logs (mongomock)."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from datetime import datetime

import pytest
from bson import ObjectId
from mongomock_motor import AsyncMongoMockClient

from scripts.backfill_ai_usage import backfill

UNTIL = datetime(2026, 10, 14, 12)
DAY = datetime(2026, 10, 10, 9)
PEDRO, MARIA = ObjectId(), ObjectId()


@pytest.fixture
def db():
    return AsyncMongoMockClient()["backfill-ai-usage"]


async def job(db, **kw):
    doc = {"createdAt": DAY, "userId": PEDRO, "estimatedCostUsd": 0.0, "inputTokens": 0, "outputTokens": 0, **kw}
    await db.resumeapplications.insert_one(doc)
    return doc


async def ledger(db, **query):
    return await db.ai_usage_events.find(query).sort([("feature", 1)]).to_list(length=None)


async def test_job_with_resume_screening_and_letter_splits_the_rest(db):
    # resume: 1000 in / 500 out on sonnet ($2/$10 per M) = $0.007 exact; $0.10 left for screening + letter.
    j = await job(db, estimatedCostUsd=0.107, inputTokens=1000, outputTokens=500, resumeLlmModel="claude-sonnet-5-5",
                  screeningLlmModel="claude-sonnet-5-5", coverLetterLlmModel="gpt-5.6-sol", coverLetterText="Dear")
    await backfill(db, until=UNTIL)
    cover, resume, screening = await ledger(db)
    assert (resume["feature"], resume["model"], resume["provider"]) == ("resume", "claude-sonnet-5-5", "anthropic")
    assert resume["costUsd"] == pytest.approx(0.007) and resume["estimated"] is False
    assert (resume["inputTokens"], resume["outputTokens"], resume["calls"]) == (1000, 500, 1)
    # weights from the task token profile: screening on sonnet $0.016, three letter calls on gpt-5.6-sol 3 x $0.118
    share = 0.016 / (0.016 + 0.354)
    assert screening["costUsd"] == pytest.approx(0.10 * share) and screening["estimated"] is True
    assert cover["costUsd"] == pytest.approx(0.10 * (1 - share)) and cover["estimated"] is True
    assert (cover["provider"], cover["calls"]) == ("openai", 3)
    for e in (resume, screening, cover):
        assert (e["userId"], e["createdAt"], e["refId"], e["source"]) == (PEDRO, DAY, str(j["_id"]), "backfill")


async def test_only_one_follow_up_step_gets_the_exact_rest(db):
    await job(db, estimatedCostUsd=0.05, coverLetterLlmModel="gpt-5.6-sol", coverLetterText="Dear")
    await backfill(db, until=UNTIL)
    [cover] = await ledger(db)
    assert (cover["feature"], cover["costUsd"], cover["estimated"]) == ("cover_letter", pytest.approx(0.05), False)


async def test_resume_cache_hit_made_no_call(db):
    await job(db, estimatedCostUsd=0.02, resumeLlmModel="claude-sonnet-5-5", screeningLlmModel="claude-sonnet-5-5")
    await backfill(db, until=UNTIL)
    [screening] = await ledger(db)
    assert (screening["feature"], screening["costUsd"]) == ("screening", pytest.approx(0.02))


async def test_unexplained_job_cost_is_kept_as_other(db):
    await job(db, estimatedCostUsd=0.03)
    await backfill(db, until=UNTIL)
    [other] = await ledger(db)
    assert (other["feature"], other["model"], other["costUsd"], other["estimated"]) == ("other", "", pytest.approx(0.03), True)


async def test_eval_rows_and_chat_logs(db):
    run_id = ObjectId()
    await db.resume_prompt_evals.insert_one({"_id": run_id, "createdAt": DAY, "userId": MARIA, "rows": [
        {"genModel": "claude-sonnet-5-5", "genInputTokens": 100, "genOutputTokens": 50, "genCostUsd": 0.0007,
         "judgeModel": "gpt-4o-mini", "judgeInputTokens": 200, "judgeOutputTokens": 20, "judgeCostUsd": 0.5},
        {"genModel": "claude-sonnet-5-5", "genCached": True, "genCostUsd": 0.0,
         "judgeModel": "gpt-4o-mini", "judgeCached": True, "judgeCostUsd": 0.0},
    ]})
    await db.aireviewruns.insert_one({"createdAt": DAY, "ranBy": PEDRO, "model": "gpt-4o-mini",
                                      "promptTokens": 1_000_000, "completionTokens": 0})
    await db.analyzechatlogs.insert_one({"createdAt": DAY, "userId": MARIA, "model": "unknown-model",
                                         "promptTokens": 10, "completionTokens": 10})
    iv = ObjectId()
    await db.interviewchatlogs.insert_one({"createdAt": DAY, "userId": PEDRO, "interviewId": iv, "model": "gpt-4o-mini",
                                           "promptTokens": 0, "completionTokens": 1_000_000})
    await backfill(db, until=UNTIL)
    by_feature = {e["feature"]: e for e in await ledger(db)}
    assert set(by_feature) == {"eval_resume", "eval_judge", "ai_review", "analyze_chat", "interview_chat"}
    assert by_feature["eval_resume"]["costUsd"] == pytest.approx(0.0007)
    assert by_feature["eval_judge"]["costUsd"] == pytest.approx(0.5)  # the stored cost wins over re-pricing
    assert by_feature["eval_judge"]["refId"] == str(run_id) and by_feature["eval_judge"]["userId"] == MARIA
    assert by_feature["ai_review"]["costUsd"] == pytest.approx(0.15) and by_feature["ai_review"]["userId"] == PEDRO
    assert by_feature["analyze_chat"]["costUsd"] is None
    assert by_feature["interview_chat"]["costUsd"] == pytest.approx(0.6)
    assert by_feature["interview_chat"]["refId"] == str(iv)


async def test_window_rerun_and_live_events(db):
    await job(db, estimatedCostUsd=0.05, coverLetterLlmModel="gpt-5.6-sol", coverLetterText="Dear")
    await job(db, createdAt=datetime(2026, 10, 14, 13), estimatedCostUsd=9.0)  # after --until: live ledger has it
    await db.ai_usage_events.insert_one({"createdAt": datetime(2026, 10, 14, 13), "feature": "resume", "source": "live"})
    first = await backfill(db, until=UNTIL)
    second = await backfill(db, until=UNTIL)
    assert first["events"] == second["events"] == 1
    assert len(await ledger(db, source="backfill")) == 1
    assert len(await ledger(db, source="live")) == 1


async def test_since_limits_the_window(db):
    await job(db, estimatedCostUsd=0.05, coverLetterLlmModel="gpt-5.6-sol", coverLetterText="Dear")
    await job(db, createdAt=datetime(2026, 9, 1), estimatedCostUsd=0.07, coverLetterLlmModel="gpt-5.6-sol",
              coverLetterText="Dear")
    summary = await backfill(db, until=UNTIL, since=datetime(2026, 10, 1))
    assert summary["events"] == 1 and summary["byFeature"]["cover_letter"]["costUsd"] == pytest.approx(0.05)


async def test_dry_run_writes_nothing(db):
    await job(db, estimatedCostUsd=0.05, coverLetterLlmModel="gpt-5.6-sol", coverLetterText="Dear")
    summary = await backfill(db, until=UNTIL, dry_run=True)
    assert summary["events"] == 1 and summary["costUsd"] == pytest.approx(0.05)
    assert await ledger(db) == []


async def test_until_defaults_to_the_first_live_event_and_cannot_pass_it(db):
    first_live = datetime(2026, 10, 14, 10)
    await db.ai_usage_events.insert_one({"createdAt": first_live, "feature": "resume", "source": "live"})
    await job(db, estimatedCostUsd=0.05, coverLetterLlmModel="gpt-5.6-sol", coverLetterText="Dear")
    await job(db, createdAt=datetime(2026, 10, 14, 11), estimatedCostUsd=9.0)  # after live recording began
    summary = await backfill(db)
    assert summary["until"] == first_live and summary["events"] == 1
    with pytest.raises(ValueError, match="live"):
        await backfill(db, until=datetime(2026, 10, 14, 12))


async def test_without_live_events_until_is_required(db):
    with pytest.raises(ValueError, match="--until"):
        await backfill(db)


async def test_jobs_and_evals_that_ran_after_until_are_left_to_the_live_ledger(db):
    late = datetime(2026, 10, 14, 13)
    letter = {"coverLetterLlmModel": "gpt-5.6-sol", "coverLetterText": "Dear"}
    await job(db, estimatedCostUsd=0.01, status="completed", completedAt=DAY, **letter)
    await job(db, estimatedCostUsd=0.02, status="completed", completedAt=late, **letter)  # queued across the deploy
    await job(db, estimatedCostUsd=0.04, status="in_progress", **letter)                  # still running
    await job(db, estimatedCostUsd=0.08, status="failed", **letter)                       # finished, no completedAt
    row = {"genModel": "claude-sonnet-5-5", "genInputTokens": 100, "genOutputTokens": 50, "genCostUsd": 0.5}
    await db.resume_prompt_evals.insert_one({"createdAt": DAY, "status": "done", "finishedAt": late, "rows": [row]})
    await db.resume_prompt_evals.insert_one({"createdAt": DAY, "status": "running", "rows": [row]})
    summary = await backfill(db, until=UNTIL)
    assert summary["byFeature"]["cover_letter"]["costUsd"] == pytest.approx(0.09)
    assert "eval_resume" not in summary["byFeature"]
