import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.bids.rate_limit import FailureLimiter, client_ip, limiter_keys


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


def test_client_ip_is_capped():
    forged = " " + "a" * 500 + " , 10.0.0.1"
    assert client_ip(SimpleNamespace(headers={"x-forwarded-for": forged}, client=None)) == "a" * 64


def test_limiter_keys_are_capped():
    assert limiter_keys("ana", "1.2.3.4") == ["user:ana", "ip:1.2.3.4"]
    assert limiter_keys("u" * 500, "9" * 500) == ["user:" + "u" * 100, "ip:" + "9" * 64]


def test_sweep_drops_stale_keys_once_past_the_threshold():
    lim, t0 = FailureLimiter(max_keys=3), datetime(2026, 10, 3, tzinfo=timezone.utc)
    for i in range(3):
        lim.reserve([f"old:{i}"], t0)
    lim.reserve(["recent"], t0 + timedelta(minutes=10))
    lim.reserve(["new"], t0 + timedelta(minutes=14))   # 4 keys before this call, but none stale yet
    assert len(lim._failures) == 5
    lim.reserve(["newer"], t0 + timedelta(minutes=16))  # past the threshold: the t0 keys are swept
    assert set(lim._failures) == {"recent", "new", "newer"}


def test_no_sweep_below_the_threshold():
    lim, t0 = FailureLimiter(max_keys=10), datetime(2026, 10, 3, tzinfo=timezone.utc)
    for i in range(3):
        lim.reserve([f"old:{i}"], t0)
    lim.reserve(["new"], t0 + timedelta(hours=1))
    assert len(lim._failures) == 4  # stale keys wait for the next sweep (or their own next attempt)


def test_default_threshold_is_ten_thousand_keys():
    assert FailureLimiter().max_keys == 10_000
