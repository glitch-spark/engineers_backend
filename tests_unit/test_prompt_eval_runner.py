"""Eval runner: stale rule, aggregation, per-row generate→judge with persistence, bounded parallelism (no DB)."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from beanie import PydanticObjectId

from app.llm_router import LlmResult
from app.models.prompt_eval import EvalRow, JudgeVerdict, ResumePromptEval
from app.prompt_eval import runner

HTML = "<html><body><p>Hi</p></body></html>"


def verdict(match=80, believability=70):
    return JudgeVerdict(matchScore=match, believabilityScore=believability, summary="ok")


def row(role="backend", status="pending", v=None, gen_cost=None, judge_cost=None, cached=False, judge_cached=False):
    return EvalRow(
        jdId=PydanticObjectId(), role=role, jdTitle=f"{role} job", jdCompany="Co", status=status, verdict=v,
        genCostUsd=gen_cost, judgeCostUsd=judge_cost, genCached=cached, judgeCached=judge_cached,
    )


def llm(model_id="anthropic:claude-sonnet-5-5", cached=False, cost=0.01, fallback=False):
    return LlmResult(
        text="", provider="anthropic", model=model_id.split(":")[1], usage={"input": 10, "output": 5},
        fallback_used=fallback, model_id=model_id, cost_usd=cost, cached=cached,
    )


# ---------- pure helpers ----------


def test_is_stale():
    now = datetime(2026, 10, 11, 12, 0)
    assert runner.is_stale("running", now - timedelta(minutes=21), now) is True
    assert runner.is_stale("running", now - timedelta(minutes=19), now) is False
    assert runner.is_stale("done", now - timedelta(hours=5), now) is False
    assert runner.is_stale("running", None, now) is False


def test_aggregate_ignores_failed_rows_and_sums_cost_over_all_rows():
    rows = [
        row("backend", "done", verdict(90, 80), gen_cost=0.02, judge_cost=0.05, cached=False),
        row("backend", "done", verdict(70, 60), gen_cost=0.0, judge_cost=0.05, cached=True),
        row("data", "done", verdict(50, 90), gen_cost=0.0, judge_cost=0.0, cached=True, judge_cached=True),
        row("ai", "failed", None, gen_cost=0.02),
    ]
    agg = runner.aggregate(rows)
    assert agg["avgMatch"] == 70.0 and agg["avgBelievability"] == pytest.approx(76.7)
    assert agg["roleScores"]["backend"].match == 80.0 and agg["roleScores"]["backend"].n == 2
    assert agg["roleScores"]["data"].believability == 90.0
    assert "ai" not in agg["roleScores"]
    assert agg["totalCostUsd"] == pytest.approx(0.14)
    assert agg["cachedRows"] == 2 and agg["cachedJudgeRows"] == 1
    assert agg["counts"] == {"total": 4, "done": 3, "failed": 1}


def test_aggregate_with_nothing_scored_gives_none_averages():
    agg = runner.aggregate([row("ai", "failed")])
    assert agg["avgMatch"] is None and agg["avgBelievability"] is None and agg["roleScores"] == {}
    assert agg["counts"] == {"total": 1, "done": 0, "failed": 1}


# ---------- _run_row ----------


@pytest.fixture
def persisted(monkeypatch):
    calls = []

    async def fake_persist(run_id, index, r):
        calls.append((index, r.status, r.model_copy(deep=True)))

    monkeypatch.setattr(runner, "_persist_row", fake_persist)
    return calls


def a_run(**over):
    base = dict(
        id=PydanticObjectId(), promptBody="Be concise", resumeModelId="anthropic:claude-sonnet-5-5",
        judgeModelId="anthropic:claude-opus-5-5", accountId=PydanticObjectId(), userId=PydanticObjectId(),
    )
    base.update(over)
    return SimpleNamespace(**base)


async def test_run_row_happy_path_persists_each_step_and_records_cache_flags(monkeypatch, persisted):
    seen = {}

    async def fake_generate(account, jd, prompt_body=None, owner=None, model_id=None):
        seen.update(jd=jd, prompt_body=prompt_body, model_id=model_id)
        return HTML, llm(cached=True, cost=0.0)

    async def fake_judge(*, resume_text, job_description, role, user, model_id):
        seen.update(resume_text=resume_text, role=role, judge_model=model_id)
        return verdict(88, 77), llm("anthropic:claude-opus-5-5", cost=0.05)

    monkeypatch.setattr(runner, "generate_resume_html", fake_generate)
    monkeypatch.setattr(runner, "judge_resume", fake_judge)
    run = a_run()
    out = await runner._run_row(run, 2, row("ml"), SimpleNamespace(), None, "JD TEXT")
    assert out.status == "done" and out.verdict.matchScore == 88
    assert out.genCached is True and out.genCostUsd == 0.0 and out.judgeCached is False and out.judgeCostUsd == 0.05
    assert out.resumeHtml == HTML and out.resumeText == "Hi"
    assert out.genModelId == "anthropic:claude-sonnet-5-5" and out.judgeModelId == "anthropic:claude-opus-5-5"
    assert out.durationMs is not None
    assert seen == {
        "jd": "JD TEXT", "prompt_body": "Be concise", "model_id": "anthropic:claude-sonnet-5-5",
        "resume_text": "Hi", "role": "ml", "judge_model": "anthropic:claude-opus-5-5",
    }
    assert [(i, s) for i, s, _ in persisted] == [(2, "generating"), (2, "judging"), (2, "done")]


async def test_run_row_judge_failure_marks_row_failed_but_keeps_the_resume(monkeypatch, persisted):
    async def fake_generate(*a, **k):
        return HTML, llm()

    async def fake_judge(**k):
        raise ValueError("judge output lacks matchScore/believabilityScore")

    monkeypatch.setattr(runner, "generate_resume_html", fake_generate)
    monkeypatch.setattr(runner, "judge_resume", fake_judge)
    out = await runner._run_row(a_run(), 0, row(), SimpleNamespace(), None, "JD")
    assert out.status == "failed" and "matchScore" in out.error
    assert out.resumeText == "Hi" and out.resumeHtml == HTML and out.verdict is None
    assert persisted[-1][1] == "failed"


async def test_run_row_generation_failure_and_missing_jd(monkeypatch, persisted):
    async def fake_generate(*a, **k):
        raise RuntimeError("No model available for resume: configure an API key")

    monkeypatch.setattr(runner, "generate_resume_html", fake_generate)
    out = await runner._run_row(a_run(), 0, row(), SimpleNamespace(), None, "JD")
    assert out.status == "failed" and "No model available" in out.error and out.resumeText is None
    out = await runner._run_row(a_run(), 1, row(), SimpleNamespace(), None, None)
    assert out.status == "failed" and out.error == "job description was deleted"


async def test_run_row_empty_prompt_is_sent_as_none(monkeypatch, persisted):
    seen = {}

    async def fake_generate(account, jd, prompt_body=None, owner=None, model_id=None):
        seen["prompt_body"] = prompt_body
        return HTML, llm()

    async def fake_judge(**k):
        return verdict(), llm()

    monkeypatch.setattr(runner, "generate_resume_html", fake_generate)
    monkeypatch.setattr(runner, "judge_resume", fake_judge)
    await runner._run_row(a_run(promptBody=""), 0, row(), SimpleNamespace(), None, "JD")
    assert seen["prompt_body"] is None


# ---------- run_eval ----------


class FakeQuery:
    def __init__(self, docs):
        self.docs = docs

    async def to_list(self):
        return self.docs


@pytest.fixture
def world(monkeypatch, persisted):
    """A run with three JDs, the profile and owner present; model calls and persistence faked."""
    jds = [SimpleNamespace(id=PydanticObjectId(), text=f"JD {i}") for i in range(3)]
    rows = [EvalRow(jdId=j.id, role=r, jdTitle=f"{r} job", jdCompany="Co") for j, r in zip(jds, ("backend", "ai", "data"))]
    # A Beanie document can't be built without an initialized collection; the runner only reads these fields.
    run = SimpleNamespace(
        id=PydanticObjectId(), status="running", userId=PydanticObjectId(), accountId=PydanticObjectId(),
        accountName="P", promptBody="x", resumeModelId=None, judgeModelId=None, rows=rows,
    )
    account = SimpleNamespace(id=run.accountId, styleTemplate="<html/>")
    finished = []

    async def fake_get_run(run_id):
        return run if run_id == run.id else None

    async def fake_get_account(account_id):
        return account

    async def fake_get_user(user_id):
        return None

    async def fake_finish(run_id, rows, started, *, error=None):
        finished.append((run_id, [r.status for r in rows], error))

    monkeypatch.setattr(ResumePromptEval, "get", staticmethod(fake_get_run))
    monkeypatch.setattr(runner.Account, "get", staticmethod(fake_get_account))
    monkeypatch.setattr(runner.UserExtra, "get", staticmethod(fake_get_user))
    monkeypatch.setattr(runner.EvalJobDescription, "find", staticmethod(lambda q: FakeQuery(jds)))
    monkeypatch.setattr(runner, "_finish", fake_finish)
    monkeypatch.setattr(runner, "_sem", None)
    return SimpleNamespace(run=run, jds=jds, finished=finished, account=account)


async def test_run_eval_finishes_done_even_when_some_rows_fail(monkeypatch, world):
    async def fake_generate(account, jd, **k):
        if jd == "JD 1":
            raise RuntimeError("boom")
        return HTML, llm()

    async def fake_judge(**k):
        return verdict(), llm()

    monkeypatch.setattr(runner, "generate_resume_html", fake_generate)
    monkeypatch.setattr(runner, "judge_resume", fake_judge)
    monkeypatch.setattr(runner, "get_settings", lambda: SimpleNamespace(PROMPT_EVAL_CONCURRENCY=4))
    await runner.run_eval(world.run.id)
    (run_id, statuses, error), = world.finished
    assert run_id == world.run.id and error is None
    assert statuses == ["done", "failed", "done"]


async def test_run_eval_respects_the_concurrency_limit(monkeypatch, world):
    in_flight = {"now": 0, "max": 0}

    async def fake_generate(account, jd, **k):
        in_flight["now"] += 1
        in_flight["max"] = max(in_flight["max"], in_flight["now"])
        await asyncio.sleep(0.01)
        in_flight["now"] -= 1
        return HTML, llm()

    async def fake_judge(**k):
        return verdict(), llm()

    monkeypatch.setattr(runner, "generate_resume_html", fake_generate)
    monkeypatch.setattr(runner, "judge_resume", fake_judge)
    monkeypatch.setattr(runner, "get_settings", lambda: SimpleNamespace(PROMPT_EVAL_CONCURRENCY=2))
    await runner.run_eval(world.run.id)
    assert in_flight["max"] == 2
    assert world.finished[0][1] == ["done", "done", "done"]


async def test_run_eval_fails_the_run_when_the_profile_is_gone(monkeypatch, world):
    async def no_account(account_id):
        return None

    monkeypatch.setattr(runner.Account, "get", staticmethod(no_account))
    await runner.run_eval(world.run.id)
    assert world.finished[0][2] == "profile no longer exists"


async def test_run_eval_ignores_unknown_or_finished_runs(monkeypatch, world):
    await runner.run_eval(PydanticObjectId())
    world.run.status = "done"
    await runner.run_eval(world.run.id)
    assert world.finished == []
