import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.bids.rate_limit import FailureLimiter, client_ip


def test_limits_after_ten_failures_and_recovers():
    lim, t0 = FailureLimiter(), datetime(2026, 10, 3, tzinfo=timezone.utc)
    for i in range(9):
        lim.fail(["user:a", "ip:1"], t0 + timedelta(seconds=i))
    assert lim.retry_after(["user:a", "ip:1"], t0 + timedelta(seconds=10)) is None
    lim.fail(["user:a", "ip:1"], t0 + timedelta(seconds=9))
    assert 0 < lim.retry_after(["user:a"], t0 + timedelta(seconds=10)) <= 900
    assert lim.retry_after(["ip:1"], t0 + timedelta(minutes=15, seconds=1)) is None
    lim.clear("user:a")
    assert lim.retry_after(["user:a"], t0 + timedelta(seconds=10)) is None


def test_client_ip_prefers_forwarded_for():
    assert client_ip(SimpleNamespace(headers={"x-forwarded-for": " 1.2.3.4, 10.0.0.1"}, client=None)) == "1.2.3.4"
    assert client_ip(SimpleNamespace(headers={}, client=SimpleNamespace(host="9.9.9.9"))) == "9.9.9.9"
    assert client_ip(SimpleNamespace(headers={}, client=None)) == "unknown"
