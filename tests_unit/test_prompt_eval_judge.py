"""Judge prompt, lenient verdict parsing, and the cached judge call (no DB, no model, no Redis)."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

import json
from types import SimpleNamespace

import pytest

from app import model_catalog as mc
from app import redis_cache
from app.llm_router import LlmResult
from app.prompt_eval import judge

GOOD = {
    "matchScore": 82,
    "believabilityScore": 74,
    "matchNotes": ["7 years React", "no Kafka"],
    "believabilityNotes": ["consistent timeline"],
    "missingRequirements": ["Kafka"],
    "redFlags": [],
    "strengths": ["led a team of 4"],
    "summary": "Strong fit. Credible.",
}


@pytest.fixture(autouse=True)
def configured(monkeypatch):
    monkeypatch.setattr(
        mc, "get_settings",
        lambda: SimpleNamespace(
            ANTHROPIC_API_KEY="sk-ant-test", OPENAI_API_KEY=None,
            RESUME_DEFAULT_MODEL="anthropic:claude-sonnet-5-5", COVER_LETTER_DEFAULT_MODEL="anthropic:claude-opus-5-5",
            SCREENING_DEFAULT_MODEL="anthropic:claude-sonnet-5-5", JUDGE_DEFAULT_MODEL="anthropic:claude-opus-5-5",
        ),
    )


# ---------- prompt ----------


def test_prompt_states_both_scores_and_the_ignore_location_rule():
    p = judge.build_judge_prompt(resume_text="RESUME BODY", job_description="JD BODY", role="backend")
    assert "IGNORE location" in p and "work-authorization" in p
    assert "matchScore" in p and "believabilityScore" in p
    for key in ("matchNotes", "believabilityNotes", "missingRequirements", "redFlags", "strengths", "summary"):
        assert f'"{key}"' in p
    assert "RESUME BODY" in p and "JD BODY" in p and "for a backend role" in p
    assert "single JSON object only" in p


def test_prompt_trims_long_inputs():
    p = judge.build_judge_prompt(resume_text="r" * 50_000, job_description="j" * 50_000, role="data")
    assert p.count("r") < 25_000 and "truncated" in p


# ---------- parsing ----------


def test_parse_plain_json():
    v = judge.parse_verdict(json.dumps(GOOD))
    assert (v.matchScore, v.believabilityScore) == (82, 74)
    assert v.missingRequirements == ["Kafka"] and v.strengths == ["led a team of 4"]
    assert v.summary == "Strong fit. Credible."


def test_parse_fenced_and_prose_wrapped_json():
    fenced = "```json\n" + json.dumps(GOOD) + "\n```"
    prose = "Here is my assessment:\n" + json.dumps(GOOD) + "\nHope this helps."
    assert judge.parse_verdict(fenced).matchScore == 82
    assert judge.parse_verdict(prose).believabilityScore == 74


def test_parse_python_literal():
    raw = "{'matchScore': 61, 'believabilityScore': 88, 'strengths': ['Go'], 'summary': 'ok'}"
    v = judge.parse_verdict(raw)
    assert (v.matchScore, v.believabilityScore, v.strengths) == (61, 88, ["Go"])


def test_parse_clamps_and_coerces_scores():
    v = judge.parse_verdict(json.dumps({"matchScore": "87%", "believabilityScore": 150}))
    assert (v.matchScore, v.believabilityScore) == (87, 100)
    v = judge.parse_verdict(json.dumps({"matchScore": -5, "believabilityScore": 72.6}))
    assert (v.matchScore, v.believabilityScore) == (0, 73)


def test_parse_accepts_key_aliases_and_bare_strings():
    raw = json.dumps({"match_score": 70, "believability": 65, "missing": "Kafka", "redFlags": [{"text": "odd dates"}]})
    v = judge.parse_verdict(raw)
    assert (v.matchScore, v.believabilityScore) == (70, 65)
    assert v.missingRequirements == ["Kafka"] and v.redFlags == ["odd dates"]
    assert v.matchNotes == [] and v.summary == ""


def test_parse_caps_list_sizes_and_item_length():
    raw = json.dumps({"matchScore": 1, "believabilityScore": 2, "strengths": ["x" * 1000] * 30})
    v = judge.parse_verdict(raw)
    assert len(v.strengths) == judge.MAX_LIST_ITEMS and len(v.strengths[0]) == judge.MAX_ITEM_CHARS


@pytest.mark.parametrize("raw", ["", "not json at all", "[1, 2]", json.dumps({"matchScore": 50}),
                                 json.dumps({"matchScore": "high", "believabilityScore": 50})])
def test_parse_rejects_missing_scores_or_non_objects(raw):
    with pytest.raises(ValueError):
        judge.parse_verdict(raw)


# ---------- judge_resume ----------


class FakeRedis:
    def __init__(self):
        self.store = {}

    async def get_json(self, key):
        return self.store.get(key)

    async def set_json(self, key, value, ttl=None):
        self.store[key] = value
        return True


@pytest.fixture
def redis(monkeypatch):
    fake = FakeRedis()
    monkeypatch.setattr(redis_cache, "get_json", fake.get_json)
    monkeypatch.setattr(redis_cache, "set_json", fake.set_json)
    return fake


def llm_result(model_id="anthropic:claude-opus-5-5", text=None, fallback=False):
    return LlmResult(
        text=text if text is not None else json.dumps(GOOD), provider="anthropic", model=model_id.split(":")[1],
        usage={"input": 100, "output": 50}, fallback_used=fallback, model_id=model_id, cost_usd=0.01,
    )


async def test_judge_resume_uses_the_judge_task_and_validates_output(monkeypatch, redis):
    calls = []

    async def fake_complete(task, prompt, user, *, model_id=None, validate=None):
        calls.append((task, prompt, model_id, validate))
        return llm_result()

    monkeypatch.setattr(judge, "complete", fake_complete)
    verdict, res = await judge.judge_resume(
        resume_text="R", job_description="J", role="ml", user=None, model_id="anthropic:claude-opus-5-5",
    )
    task, prompt, model_id, validate = calls[0]
    assert task == "judge" and model_id == "anthropic:claude-opus-5-5" and "for a ml role" in prompt
    with pytest.raises(ValueError):
        validate("garbage")
    validate(json.dumps(GOOD))
    assert verdict.matchScore == 82 and res.cached is False and res.cost_usd == 0.01


async def test_judge_resume_stores_the_verdict_under_the_answering_lane(monkeypatch, redis):
    # Sonnet requested, but the task default (Opus, next in the chain) answered after a fallback.
    async def fake_complete(task, prompt, user, *, model_id=None, validate=None):
        return llm_result(model_id="anthropic:claude-opus-5-5", fallback=True)

    monkeypatch.setattr(judge, "complete", fake_complete)
    await judge.judge_resume(resume_text="R", job_description="J", role="ai", user=None, model_id="anthropic:claude-sonnet-5-5")
    prompt = judge.build_judge_prompt(resume_text="R", job_description="J", role="ai")
    sonnet = mc.cache_lane(mc.get_model("anthropic:claude-sonnet-5-5"), "judge")
    opus = mc.cache_lane(mc.get_model("anthropic:claude-opus-5-5"), "judge")
    assert judge._cache_key(prompt, opus) in redis.store
    assert judge._cache_key(prompt, sonnet) not in redis.store, "the requested lane must stay empty so Sonnet is retried next time"
    stored = redis.store[judge._cache_key(prompt, opus)]
    assert stored["verdict"]["matchScore"] == 82 and stored["fallback"] is True and stored["model_id"] == "anthropic:claude-opus-5-5"


async def test_judge_resume_redis_hit_skips_the_model(monkeypatch, redis):
    prompt = judge.build_judge_prompt(resume_text="R", job_description="J", role="ai")
    lane = mc.cache_lane(mc.get_model("anthropic:claude-opus-5-5"), "judge")
    redis.store[judge._cache_key(prompt, lane)] = {"verdict": GOOD, "model": "claude-opus-5-5"}

    async def boom(*a, **k):
        raise AssertionError("model must not be called on a cache hit")

    monkeypatch.setattr(judge, "complete", boom)
    verdict, res = await judge.judge_resume(resume_text="R", job_description="J", role="ai", user=None, model_id=None)
    assert verdict.believabilityScore == 74
    assert res.cached is True and res.cost_usd == 0.0 and res.model_id == "anthropic:claude-opus-5-5"


async def test_judge_resume_ignores_a_corrupt_cached_value(monkeypatch, redis):
    prompt = judge.build_judge_prompt(resume_text="R", job_description="J", role="ai")
    lane = mc.cache_lane(mc.get_model("anthropic:claude-opus-5-5"), "judge")
    redis.store[judge._cache_key(prompt, lane)] = {"verdict": {"matchScore": "nope"}}

    async def fake_complete(task, prompt, user, *, model_id=None, validate=None):
        return llm_result()

    monkeypatch.setattr(judge, "complete", fake_complete)
    verdict, res = await judge.judge_resume(resume_text="R", job_description="J", role="ai", user=None, model_id=None)
    assert verdict.matchScore == 82 and res.cached is False


def test_prompt_version_changes_the_cache_key(monkeypatch):
    before = judge._cache_key("p", "lane")
    monkeypatch.setattr(judge, "JUDGE_PROMPT_VERSION", "resume-judge-v2")
    assert judge._cache_key("p", "lane") != before
