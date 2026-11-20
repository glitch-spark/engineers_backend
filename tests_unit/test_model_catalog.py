"""Unit tests for the model catalog: providers, per-task presets, defaults, validation, fallback chain, cost."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from datetime import date
from types import SimpleNamespace

import pytest

from app import model_catalog as mc


def settings(**over):
    base = dict(
        ANTHROPIC_API_KEY="sk-ant-test",
        OPENAI_API_KEY="sk-openai-test",
        RESUME_DEFAULT_MODEL="anthropic:claude-sonnet-5-5",
        COVER_LETTER_DEFAULT_MODEL="anthropic:claude-opus-5-5",
        SCREENING_DEFAULT_MODEL="anthropic:claude-sonnet-5-5",
        JUDGE_DEFAULT_MODEL="anthropic:claude-opus-5-5",
    )
    base.update(over)
    return SimpleNamespace(**base)


@pytest.fixture
def use_settings(monkeypatch):
    def apply(**over):
        monkeypatch.setattr(mc, "get_settings", lambda: settings(**over))

    apply()
    return apply


@pytest.fixture(autouse=True)
def today(monkeypatch):
    """Pins "today" (before GPT-4o mini's retirement); call it with another date to move it."""
    def move(day):
        monkeypatch.setattr(mc, "_today", lambda: day)

    move(date(2026, 11, 3))
    return move


def ids(models):
    return [m.id for m in models]


def provider_ids(task, provider):
    return [m.id for m in mc.available_models(task, None) if m.provider == provider]


A = "anthropic:claude-"
O = "openai:"

# The agreed presets: ordered, first entry is the provider's recommended model for the task.
EXPECTED = {
    ("resume", "anthropic"): [A + "sonnet-5-5", A + "opus-5-5", A + "haiku-5-5", A + "haiku-4-5", A + "sonnet-5"],
    ("cover_letter", "anthropic"): [
        A + "opus-5-5", A + "fable-5-1", A + "sonnet-5-5", A + "opus-5", A + "opus-4-8",
    ],
    ("screening", "anthropic"): [A + "sonnet-5-5", A + "opus-5-5", A + "haiku-4-5", A + "sonnet-5"],
    # GPT-6.1 Sol is turned off, so it is listed nowhere.
    ("resume", "openai"): [O + "gpt-5-mini", O + "gpt-5.4-mini", O + "gpt-4o-mini", O + "gpt-6-luna"],
    ("cover_letter", "openai"): [O + "gpt-5.6-sol", O + "gpt-6-astra", O + "gpt-5.5", O + "gpt-5.6-terra"],
    ("screening", "openai"): [O + "gpt-5.4-mini", O + "gpt-6-astra", O + "gpt-5-mini", O + "gpt-6-luna"],
    # Judge (prompt evaluator): strong models only, fresh-context scoring.
    ("judge", "anthropic"): [A + "opus-5-5", A + "fable-5-1", A + "sonnet-5-5"],
    ("judge", "openai"): [O + "gpt-6-astra", O + "gpt-5.6-sol", O + "gpt-5.5"],
}


@pytest.mark.parametrize("task,provider", list(EXPECTED))
def test_presets_match_the_agreed_lists(use_settings, task, provider):
    assert provider_ids(task, provider) == EXPECTED[(task, provider)]


def test_providers_are_listed_anthropic_first_and_only_two(use_settings):
    got = mc.available_models("resume", None)
    assert [m.provider for m in got][0] == "anthropic"
    assert {m.provider for m in got} == {"anthropic", "openai"}


def test_provider_needs_its_api_key(use_settings):
    use_settings(ANTHROPIC_API_KEY=None)
    assert {m.provider for m in mc.available_models("resume", None)} == {"openai"}
    use_settings(OPENAI_API_KEY="  ")
    assert {m.provider for m in mc.available_models("resume", None)} == {"anthropic"}
    use_settings(ANTHROPIC_API_KEY=None, OPENAI_API_KEY=None)
    assert mc.available_models("resume", None) == []


def test_removed_nvidia_model_ids_are_rejected_and_count_as_off(use_settings):
    with pytest.raises(mc.ModelNotAvailable, match="removed"):
        mc.validate_requested("resume", "free:glm-5.3")
    assert mc.is_inactive("free:glm-5.3")
    assert not mc.is_inactive("bogus:model")


def test_fable_is_cover_letter_only_and_haiku_never_writes_letters(use_settings):
    assert A + "fable-5-1" not in ids(mc.available_models("resume", None))
    assert A + "fable-5-1" not in ids(mc.available_models("screening", None))
    assert A + "haiku-4-5" not in ids(mc.available_models("cover_letter", None))


def test_gpt_4o_mini_is_resume_only(use_settings):
    assert O + "gpt-4o-mini" in ids(mc.available_models("resume", None))
    for task in ("cover_letter", "screening"):
        with pytest.raises(mc.ModelNotAvailable):
            mc.validate_requested(task, O + "gpt-4o-mini", None)


@pytest.mark.parametrize("day", [date(2027, 1, 14), date(2027, 6, 1)])
def test_gpt_4o_mini_is_off_from_its_retirement_date(use_settings, today, day):
    today(date(2027, 1, 13))
    assert O + "gpt-4o-mini" in ids(mc.available_models("resume", None))
    today(day)
    assert O + "gpt-4o-mini" not in ids(mc.available_models("resume", None))
    assert mc.is_inactive(O + "gpt-4o-mini")
    with pytest.raises(mc.ModelNotAvailable, match="retired by OpenAI"):
        mc.validate_requested("resume", O + "gpt-4o-mini", None)
    assert O + "gpt-4o-mini" not in ids(mc.fallback_chain("resume", O + "gpt-4o-mini"))
    assert mc.api_model_cost("gpt-4o-mini", {"input": 1_000_000}) == pytest.approx(0.15)  # old usage still priced


@pytest.mark.parametrize("day,default,offered", [
    (date(2026, 12, 31), "gpt-4o-mini", ["gpt-6-luna", "gpt-5-mini", "gpt-5.4-mini", "gpt-4o-mini"]),
    (date(2027, 1, 1), "gpt-6-luna", ["gpt-6-luna", "gpt-5-mini", "gpt-5.4-mini", "gpt-4o-mini"]),
    (date(2027, 1, 14), "gpt-6-luna", ["gpt-6-luna", "gpt-5-mini", "gpt-5.4-mini"]),
])
def test_job_applies_resume_models_and_default_by_date(use_settings, today, day, default, offered):
    today(day)
    assert mc.job_applies_resume_default() == O + default  # Haiku 5.5 is offered, never the default
    assert ids(mc.job_applies_resume_models()) == [O + "gpt-6-luna", A + "haiku-5-5"] + [O + m for m in offered[1:]]
    use_settings(OPENAI_API_KEY=None)
    assert ids(mc.job_applies_resume_models()) == [A + "haiku-5-5"]
    use_settings(OPENAI_API_KEY=None, ANTHROPIC_API_KEY=None)
    assert mc.job_applies_resume_models() == []


def test_job_applies_resume_models_are_resume_presets(use_settings):
    resume = ids(mc.available_models("resume", None))
    assert all(m in resume for m in mc.JOB_APPLIES_RESUME_MODELS)  # so the runner's fallback chain accepts them


def test_defaults_per_task(use_settings):
    assert mc.default_model_id("resume", None) == A + "sonnet-5-5"
    assert mc.default_model_id("screening", None) == A + "sonnet-5-5"
    assert mc.default_model_id("cover_letter", None) == A + "opus-5-5"
    assert mc.default_model_id("judge", None) == A + "opus-5-5"


def test_default_falls_back_to_the_openai_recommendation_without_anthropic(use_settings):
    use_settings(ANTHROPIC_API_KEY=None)
    assert mc.default_model_id("resume", None) == O + "gpt-5-mini"
    assert mc.default_model_id("cover_letter", None) == O + "gpt-5.6-sol"
    assert mc.default_model_id("screening", None) == O + "gpt-5.4-mini"
    assert mc.default_model_id("judge", None) == O + "gpt-6-astra"


def test_judge_task_effort_is_high_and_has_a_token_profile(use_settings):
    assert mc.TASK_EFFORT["judge"] == "high"
    tokens_in, tokens_out = mc.TASK_TOKEN_PROFILE["judge"]
    assert tokens_in > 0 and tokens_out > 0
    assert mc.cache_lane(mc.get_model(A + "opus-5-5", None), "judge", None) == "anthropic:claude-opus-5-5:high"


def test_haiku_cannot_judge(use_settings):
    with pytest.raises(mc.ModelNotAvailable):
        mc.validate_requested("judge", A + "haiku-4-5", None)
    use_settings(ANTHROPIC_API_KEY=None, OPENAI_API_KEY=None)
    assert mc.default_model_id("resume", None) is None


def test_configured_default_must_be_a_usable_preset(use_settings):
    use_settings(RESUME_DEFAULT_MODEL="anthropic:claude-fable-5-1")  # not a resume preset
    assert mc.default_model_id("resume", None) == A + "sonnet-5-5"


def test_validate_requested_blank_returns_none(use_settings):
    assert mc.validate_requested("resume", None, None) is None
    assert mc.validate_requested("resume", "  ", None) is None


def test_validate_requested_unknown_id_raises(use_settings):
    with pytest.raises(mc.ModelNotAvailable) as exc:
        mc.validate_requested("resume", "anthropic:nope", None)
    assert "anthropic:nope" in str(exc.value)


def test_validate_requested_missing_provider_key_raises(use_settings):
    use_settings(ANTHROPIC_API_KEY=None)
    with pytest.raises(mc.ModelNotAvailable) as exc:
        mc.validate_requested("resume", A + "sonnet-5-5", None)
    assert "not configured" in str(exc.value)


@pytest.mark.parametrize(
    "task,model_id",
    [("cover_letter", A + "haiku-4-5"), ("resume", A + "fable-5-1"), ("resume", O + "gpt-6-astra")],
)
def test_validate_requested_wrong_task_raises(use_settings, task, model_id):
    with pytest.raises(mc.ModelNotAvailable):
        mc.validate_requested(task, model_id, None)


def test_validate_requested_returns_spec(use_settings):
    spec = mc.validate_requested("resume", " anthropic:claude-sonnet-5-5 ", None)
    assert spec.api_model == "claude-sonnet-5-5" and spec.provider == "anthropic"


def test_fallback_chain_order_and_dedup(use_settings):
    assert ids(mc.fallback_chain("resume", A + "haiku-4-5", None)) == [
        A + "haiku-4-5", A + "sonnet-5-5", O + "gpt-5-mini",
    ]
    assert ids(mc.fallback_chain("resume", A + "sonnet-5-5", None)) == [A + "sonnet-5-5", O + "gpt-5-mini"]
    assert ids(mc.fallback_chain("resume", O + "gpt-5.4-mini", None)) == [
        O + "gpt-5.4-mini", A + "sonnet-5-5", O + "gpt-5-mini",
    ]
    assert ids(mc.fallback_chain("resume", O + "gpt-5-mini", None)) == [O + "gpt-5-mini", A + "sonnet-5-5"]
    assert ids(mc.fallback_chain("cover_letter", None, None)) == [A + "opus-5-5", O + "gpt-5.6-sol"]


def test_fallback_chain_with_one_provider(use_settings):
    use_settings(ANTHROPIC_API_KEY=None)
    assert ids(mc.fallback_chain("resume", O + "gpt-6-luna", None)) == [O + "gpt-6-luna", O + "gpt-5-mini"]


def test_turned_off_model_is_never_offered_defaulted_or_fallen_back_to(use_settings):
    sol = O + "gpt-6.1-sol"
    assert mc.is_inactive(sol) and not mc.is_inactive(O + "gpt-5-mini")
    assert not mc.is_inactive("anthropic:nope") and not mc.is_inactive(None)
    use_settings(ANTHROPIC_API_KEY=None, RESUME_DEFAULT_MODEL=sol)
    for task in ("resume", "cover_letter", "screening"):
        assert sol not in ids(mc.available_models(task, None))
        assert sol not in ids(mc.fallback_chain(task, sol, None))
        with pytest.raises(mc.ModelNotAvailable, match="GPT-6.1 Sol is turned off"):
            mc.validate_requested(task, sol, None)
    assert mc.default_model_id("resume", None) == O + "gpt-5-mini"
    assert ids(mc.fallback_chain("resume", sol, None)) == [O + "gpt-5-mini"]


def test_usage_cost_uses_official_prices(use_settings):
    one_m = {"input": 1_000_000, "output": 1_000_000}
    prices = {
        A + "sonnet-5-5": 12.0, A + "opus-5-5": 24.0, A + "fable-5-1": 60.0, A + "haiku-4-5": 6.0, A + "haiku-5-5": 0.6,
        A + "opus-5": 30.0, A + "opus-4-8": 30.0, A + "sonnet-5": 12.0,
        O + "gpt-6.1-sol": 12.0, O + "gpt-6-astra": 60.0, O + "gpt-6-luna": 0.6, O + "gpt-5.6-sol": 24.0,
        O + "gpt-5.5": 35.0, O + "gpt-5.6-terra": 14.0, O + "gpt-5.4-mini": 5.25, O + "gpt-5-mini": 2.25,
        O + "gpt-4o-mini": 0.75,
    }
    for model_id, expected in prices.items():
        assert mc.usage_cost(mc.get_model(model_id, None), one_m) == pytest.approx(expected), model_id


def test_estimate_call_cost(use_settings):
    assert mc.estimate_call_cost(mc.get_model(A + "sonnet-5-5", None), "resume") == pytest.approx(0.062)
    assert mc.estimate_call_cost(mc.get_model(O + "gpt-6-luna", None), "resume") == pytest.approx(0.0031)


def test_cache_lane(use_settings):
    assert mc.cache_lane(mc.get_model(O + "gpt-5-mini", None), "resume", None) == "openai:gpt-5-mini:low"
    assert mc.cache_lane(mc.get_model(O + "gpt-6.1-sol", None), "cover_letter", None) == "openai:gpt-6.1-sol:high"
    assert mc.cache_lane(mc.get_model(A + "sonnet-5-5", None), "resume", None) == "anthropic:claude-sonnet-5-5:low"
    assert mc.cache_lane(mc.get_model(A + "haiku-4-5", None), "resume", None) == "anthropic:claude-haiku-4-5:none"
    assert mc.cache_lane(mc.get_model(A + "haiku-5-5", None), "resume", None) == "anthropic:claude-haiku-5-5:low"
    assert mc.cache_lane(mc.get_model(O + "gpt-4o-mini", None), "resume", None) == "openai:gpt-4o-mini:none"


def test_public_view_has_tag_and_marks_each_providers_recommendation(use_settings):
    default_id = mc.default_model_id("resume", None)
    views = {m.id: mc.public_view(m, "resume", default_id) for m in mc.available_models("resume", None)}
    sonnet = views[A + "sonnet-5-5"]
    assert sonnet["isDefault"] is True and sonnet["recommended"] is True and sonnet["tag"]
    assert set(sonnet) == {"id", "label", "provider", "tier", "bestFor", "estCostUsd", "isDefault", "recommended", "tag"}
    assert views[O + "gpt-5-mini"]["recommended"] is True and views[O + "gpt-5-mini"]["isDefault"] is False
    assert views[A + "opus-5-5"]["recommended"] is False
    assert views[O + "gpt-6-luna"]["tag"] == "Cheapest"


def test_haiku_5_5_writes_resumes_only(use_settings):
    assert mc.validate_requested("resume", A + "haiku-5-5", None).api_model == "claude-haiku-5-5"
    assert mc.preset_tag("resume", A + "haiku-5-5") == "Cheapest"
    for task in ("cover_letter", "screening", "judge"):
        with pytest.raises(mc.ModelNotAvailable):
            mc.validate_requested(task, A + "haiku-5-5", None)
