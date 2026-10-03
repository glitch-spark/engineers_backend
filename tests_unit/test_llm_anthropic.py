"""Unit tests for the Anthropic adapter (SDK mocked)."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from types import SimpleNamespace

import pytest

from app import llm_anthropic


def make_client(message, calls):
    class FakeStream:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get_final_message(self):
            return message

    class FakeMessages:
        def stream(self, **kwargs):
            calls.append(kwargs)
            return FakeStream()

    class FakeClient:
        def __init__(self, **kwargs):
            calls.append({"client_kwargs": kwargs})
            self.messages = FakeMessages()

    return FakeClient


def msg(content, stop_reason="end_turn", usage=(11, 22)):
    return SimpleNamespace(
        content=content,
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=usage[0], output_tokens=usage[1]),
    )


@pytest.fixture
def key(monkeypatch):
    monkeypatch.setattr(
        llm_anthropic, "get_settings", lambda: SimpleNamespace(ANTHROPIC_API_KEY="sk-ant-test")
    )


async def test_returns_text_blocks_only_and_usage(monkeypatch, key):
    calls = []
    content = [
        SimpleNamespace(type="thinking", thinking="hmm"),
        SimpleNamespace(type="text", text="Hello "),
        SimpleNamespace(type="text", text="world"),
    ]
    monkeypatch.setattr(llm_anthropic, "AsyncAnthropic", make_client(msg(content), calls))
    text, usage = await llm_anthropic.anthropic_complete("claude-sonnet-5-5", "hi")
    assert text == "Hello world"
    assert usage == {"input": 11, "output": 22}


async def test_request_has_no_thinking_or_sampling_params(monkeypatch, key):
    calls = []
    monkeypatch.setattr(
        llm_anthropic, "AsyncAnthropic", make_client(msg([SimpleNamespace(type="text", text="x")]), calls)
    )
    await llm_anthropic.anthropic_complete("claude-sonnet-5-5", "the prompt")
    request = calls[-1]
    for banned in ("thinking", "temperature", "top_p", "tool_choice"):
        assert banned not in request
    assert request["model"] == "claude-sonnet-5-5"
    assert request["max_tokens"] == 16000
    assert request["messages"] == [{"role": "user", "content": "the prompt"}]


async def test_effort_sent_only_when_given(monkeypatch, key):
    calls = []
    monkeypatch.setattr(
        llm_anthropic, "AsyncAnthropic", make_client(msg([SimpleNamespace(type="text", text="x")]), calls)
    )
    await llm_anthropic.anthropic_complete("claude-sonnet-5-5", "p", effort="low")
    assert calls[-1]["output_config"] == {"effort": "low"}
    await llm_anthropic.anthropic_complete("claude-haiku-4-5", "p")
    assert "output_config" not in calls[-1]


async def test_refusal_raises_model_refused(monkeypatch, key):
    monkeypatch.setattr(llm_anthropic, "AsyncAnthropic", make_client(msg([], stop_reason="refusal"), []))
    with pytest.raises(llm_anthropic.ModelRefused):
        await llm_anthropic.anthropic_complete("claude-sonnet-5-5", "p")


async def test_max_tokens_stop_raises_output_truncated(monkeypatch, key):
    truncated = msg([SimpleNamespace(type="text", text="<body>cut")], stop_reason="max_tokens")
    monkeypatch.setattr(llm_anthropic, "AsyncAnthropic", make_client(truncated, []))
    with pytest.raises(llm_anthropic.OutputTruncated):
        await llm_anthropic.anthropic_complete("claude-sonnet-5-5", "p")


async def test_missing_key_raises_runtime_error(monkeypatch):
    monkeypatch.setattr(
        llm_anthropic, "get_settings", lambda: SimpleNamespace(ANTHROPIC_API_KEY=None)
    )
    with pytest.raises(RuntimeError):
        await llm_anthropic.anthropic_complete("claude-sonnet-5-5", "p")
