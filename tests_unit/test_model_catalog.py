"""Unit tests for the model catalog: availability, defaults, validation, fallback chain, cost."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from types import SimpleNamespace

import pytest

from app import model_catalog as mc


def settings(**over):
    base = dict(
        ANTHROPIC_API_KEY="sk-ant-test",
        OPENAI_API_KEY="sk-openai-test",
        RESUME_OPENAI_MODEL="gpt-5-mini",
        RESUME_REASONING_EFFORT="low",
        RESUME_DEFAULT_MODEL="anthropic:claude-sonnet-5-5",
        COVER_LETTER_DEFAULT_MODEL="anthropic:claude-opus-5-5",
        SCREENING_DEFAULT_MODEL="anthropic:claude-sonnet-5-5",
    )
    base.update(over)
    return SimpleNamespace(**base)


@pytest.fixture
def use_settings(monkeypatch):
    def apply(**over):
        monkeypatch.setattr(mc, "get_settings", lambda: settings(**over))

    apply()
    return apply


def user(model_id="glm-5.2", key="nvapi-x", max_tokens=None):
    return SimpleNamespace(freeLlmModelId=model_id, freeLlmApiKey=key, freeLlmMaxTokens=max_tokens)


def ids(models):
    return [m.id for m in models]


def test_anthropic_models_need_api_key(use_settings):
    use_settings(ANTHROPIC_API_KEY=None)
    assert not [i for i in ids(mc.available_models("resume", None)) if i.startswith("anthropic:")]
    use_settings()
    got = ids(mc.available_models("cover_letter", None)) + ids(mc.available_models("resume", None))
    for needed in ("haiku-4-5", "sonnet-5-5", "opus-5-5", "fable-5-1"):
        assert f"anthropic:claude-{needed}" in got


def test_free_models_need_user_key(use_settings):
    assert not [i for i in ids(mc.available_models("resume", None)) if i.startswith("free:")]
    assert not [i for i in ids(mc.available_models("resume", user(key="  "))) if i.startswith("free:")]
    assert "free:glm-5.2" in ids(mc.available_models("resume", user()))


def test_cover_letter_excludes_free_and_haiku(use_settings):
    got = ids(mc.available_models("cover_letter", user()))
    assert not [i for i in got if i.startswith("free:") or "haiku" in i]
    assert "anthropic:claude-fable-5-1" in got


def test_defaults_per_task(use_settings):
    assert mc.default_model_id("resume", None) == "anthropic:claude-sonnet-5-5"
    assert mc.default_model_id("screening", None) == "anthropic:claude-sonnet-5-5"
    assert mc.default_model_id("cover_letter", None) == "anthropic:claude-opus-5-5"


def test_default_prefers_saved_free_preset_for_resume_and_screening_not_cover_letter(use_settings):
    u = user()
    assert mc.default_model_id("resume", u) == "free:glm-5.2"
    assert mc.default_model_id("screening", u) == "free:glm-5.2"
    assert mc.default_model_id("cover_letter", u) == "anthropic:claude-opus-5-5"


def test_default_falls_back_to_openai_without_anthropic_key(use_settings):
    use_settings(ANTHROPIC_API_KEY=None)
    assert mc.default_model_id("resume", None) == "openai:gpt-5-mini"
    use_settings(ANTHROPIC_API_KEY=None, OPENAI_API_KEY=None)
    assert mc.default_model_id("resume", None) is None


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
        mc.validate_requested("resume", "anthropic:claude-sonnet-5-5", None)
    assert "not configured" in str(exc.value)


def test_validate_requested_wrong_task_raises(use_settings):
    with pytest.raises(mc.ModelNotAvailable):
        mc.validate_requested("cover_letter", "anthropic:claude-haiku-4-5", None)


def test_validate_requested_returns_spec(use_settings):
    spec = mc.validate_requested("resume", " anthropic:claude-sonnet-5-5 ", None)
    assert spec.api_model == "claude-sonnet-5-5" and spec.provider == "anthropic"


def test_fallback_chain_order_and_dedup(use_settings):
    assert ids(mc.fallback_chain("resume", "anthropic:claude-haiku-4-5", None)) == [
        "anthropic:claude-haiku-4-5",
        "anthropic:claude-sonnet-5-5",
        "openai:gpt-5-mini",
    ]
    assert ids(mc.fallback_chain("resume", "anthropic:claude-sonnet-5-5", None)) == [
        "anthropic:claude-sonnet-5-5",
        "openai:gpt-5-mini",
    ]
    assert ids(mc.fallback_chain("resume", None, user())) == [
        "free:glm-5.2",
        "anthropic:claude-sonnet-5-5",
        "openai:gpt-5-mini",
    ]


def test_usage_cost(use_settings):
    one_m = {"input": 1_000_000, "output": 1_000_000}
    sonnet = mc.get_model("anthropic:claude-sonnet-5-5", None)
    assert mc.usage_cost(sonnet, one_m) == pytest.approx(12.0)
    assert mc.usage_cost(mc.get_model("openai:gpt-5-mini", None), one_m) is None
    assert mc.usage_cost(mc.get_model("free:glm-5.2", user()), one_m) == 0.0


def test_estimate_call_cost_resume_sonnet(use_settings):
    sonnet = mc.get_model("anthropic:claude-sonnet-5-5", None)
    assert mc.estimate_call_cost(sonnet, "resume") == pytest.approx(0.062)


def test_cache_lane(use_settings):
    u = user()
    assert mc.cache_lane(mc.get_model("openai:gpt-5-mini", None), "resume", u) == "openai:gpt-5-mini:low"
    assert mc.cache_lane(mc.get_model("free:glm-5.2", u), "resume", u) == "free:z-ai/glm-5.2:16384"
    assert (
        mc.cache_lane(mc.get_model("anthropic:claude-sonnet-5-5", None), "resume", u)
        == "anthropic:claude-sonnet-5-5:low"
    )
    assert (
        mc.cache_lane(mc.get_model("anthropic:claude-haiku-4-5", None), "resume", u)
        == "anthropic:claude-haiku-4-5:none"
    )


def test_public_view_shape(use_settings):
    spec = mc.get_model("anthropic:claude-sonnet-5-5", None)
    view = mc.public_view(spec, "resume", "anthropic:claude-sonnet-5-5")
    assert view == {
        "id": "anthropic:claude-sonnet-5-5",
        "label": spec.label,
        "provider": "anthropic",
        "tier": "balanced",
        "bestFor": spec.best_for,
        "estCostUsd": pytest.approx(0.062),
        "isDefault": True,
    }
