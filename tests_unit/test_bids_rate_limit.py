import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.bids.rate_limit import FailureLimiter, client_ip


def test_limits_after_ten_attempts_and_recovers():
    lim, t0 = FailureLimiter(), datetime(2026, 10, 3, tzinfo=timezone.utc)
    for i in range(9):
        assert lim.reserve(["user:a", "ip:1"], t0 + timedelta(seconds=i)) is None
    assert lim.reserve(["user:a", "ip:1"], t0 + timedelta(seconds=9)) is None  # the 10th is allowed
    wait = lim.reserve(["user:a"], t0 + timedelta(seconds=10))  # the 11th is blocked and not recorded
    assert 0 < wait <= 900
    assert len(lim._failures["user:a"]) == 10
    assert lim.reserve(["ip:1"], t0 + timedelta(minutes=15, seconds=1)) is None
    lim.clear("user:a")
    assert lim.reserve(["user:a"], t0 + timedelta(seconds=10)) is None


def test_release_gives_the_attempt_back():
    lim, t0 = FailureLimiter(), datetime(2026, 10, 3, tzinfo=timezone.utc)
    for i in range(10):
        t = t0 + timedelta(seconds=i)
        assert lim.reserve(["ip:1"], t) is None
        lim.release(["ip:1"], t)
    assert lim.reserve(["ip:1"], t0 + timedelta(seconds=11)) is None
    assert len(lim._failures["ip:1"]) == 1


def test_client_ip_prefers_forwarded_for():
    assert client_ip(SimpleNamespace(headers={"x-forwarded-for": " 1.2.3.4, 10.0.0.1"}, client=None)) == "1.2.3.4"
    assert client_ip(SimpleNamespace(headers={}, client=SimpleNamespace(host="9.9.9.9"))) == "9.9.9.9"
    assert client_ip(SimpleNamespace(headers={}, client=None)) == "unknown"
