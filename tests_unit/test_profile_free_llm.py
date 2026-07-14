"""Unit tests for free LLM profile fields and verification state."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from types import SimpleNamespace

from app.routers.profile import FreeLlmSettingsUpdate, _apply_free_llm_settings, _free_llm_profile_fields


def test_free_llm_profile_fields_masks_key_and_verified():
    user = SimpleNamespace(
        freeLlmModelId="minimax-m3",
        freeLlmMaxTokens=8192,
        freeLlmApiKey="nvapi-secret-key-1234",
        freeLlmKeyVerified=True,
    )
    fields = _free_llm_profile_fields(user)
    assert fields["freeLlmApiKeySet"] is True
    assert fields["freeLlmApiKeyHint"] == "••••1234"
    assert fields["freeLlmKeyVerified"] is True


def test_free_llm_profile_fields_verified_false_without_key():
    user = SimpleNamespace(
        freeLlmModelId="minimax-m3",
        freeLlmMaxTokens=8192,
        freeLlmApiKey=None,
        freeLlmKeyVerified=True,
    )
    fields = _free_llm_profile_fields(user)
    assert fields["freeLlmKeyVerified"] is False


def test_apply_free_llm_settings_resets_verified_on_key_change():
    user = SimpleNamespace(
        freeLlmModelId="minimax-m3",
        freeLlmMaxTokens=8192,
        freeLlmApiKey="nvapi-old-key",
        freeLlmKeyVerified=True,
    )
    _apply_free_llm_settings(user, FreeLlmSettingsUpdate(freeLlmApiKey="nvapi-new-key"))
    assert user.freeLlmApiKey == "nvapi-new-key"
    assert user.freeLlmKeyVerified is False
