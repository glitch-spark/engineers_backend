"""app.redis_cache: optional, failure-tolerant JSON cache with a long TTL (no Redis server)."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

import json
from types import SimpleNamespace

import pytest

from app import redis_cache as rc


class FakeRedis:
    def __init__(self, fail=False):
        self.store = {}
        self.ttls = {}
        self.fail = fail
        self.calls = 0

    async def get(self, key):
        self.calls += 1
        if self.fail:
            raise ConnectionError("down")
        return self.store.get(key)

    async def set(self, key, value, ex=None):
        self.calls += 1
        if self.fail:
            raise ConnectionError("down")
        self.store[key] = value
        self.ttls[key] = ex

    async def delete(self, key):
        self.calls += 1
        self.store.pop(key, None)

    async def aclose(self):
        pass


@pytest.fixture
def fake(monkeypatch):
    client = FakeRedis()
    monkeypatch.setattr(rc, "get_settings", lambda: SimpleNamespace(REDIS_URL="redis://x", LLM_CACHE_TTL_SECONDS=2592000))
    monkeypatch.setattr(rc, "_get_client", lambda: client)
    monkeypatch.setattr(rc, "_disabled_until", 0.0)
    return client


def test_cache_key_is_namespaced_and_stable():
    a = rc.cache_key("resume", "lane", "prompt")
    assert a.startswith("llm:resume:") and len(a.split(":")[-1]) == 64
    assert a == rc.cache_key("resume", "lane", "prompt")
    assert a != rc.cache_key("resume", "lane", "prompt2")
    assert a != rc.cache_key("judge", "lane", "prompt")


async def test_disabled_without_url_never_builds_a_client(monkeypatch):
    monkeypatch.setattr(rc, "get_settings", lambda: SimpleNamespace(REDIS_URL=None, LLM_CACHE_TTL_SECONDS=1))

    def boom():
        raise AssertionError("client must not be built")

    monkeypatch.setattr(rc, "_get_client", boom)
    assert rc.enabled() is False
    assert await rc.get_json("k") is None
    assert await rc.set_json("k", {"a": 1}) is False


async def test_round_trip_uses_the_configured_ttl(fake):
    assert await rc.set_json("k", {"output": "<body/>", "model": "m"}) is True
    assert fake.ttls["k"] == 2592000
    assert await rc.get_json("k") == {"output": "<body/>", "model": "m"}
    assert await rc.get_json("missing") is None


async def test_explicit_ttl_wins(fake):
    await rc.set_json("k", {"a": 1}, ttl=5)
    assert fake.ttls["k"] == 5


async def test_corrupt_or_non_object_values_are_misses(fake):
    fake.store["bad"] = "{not json"
    fake.store["list"] = json.dumps([1, 2])
    assert await rc.get_json("bad") is None
    assert await rc.get_json("list") is None


async def test_connection_error_is_a_miss_and_backs_off(monkeypatch, fake):
    fake.fail = True
    assert await rc.get_json("k") is None
    assert fake.calls == 1
    assert rc._disabled_until > 0
    # While backed off, Redis is not touched at all.
    assert await rc.get_json("k") is None
    assert await rc.set_json("k", {"a": 1}) is False
    assert fake.calls == 1


async def test_delete_and_close_are_safe(fake):
    await rc.set_json("k", {"a": 1})
    await rc.delete("k")
    assert "k" not in fake.store
    await rc.close()
