import os
os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from datetime import datetime, timedelta, timezone

import pytest

from app.bids.upload_keys import build_upload_key, job_slug

NOW = datetime(2026, 10, 3, 15, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize("raw,expected", [
    ("acme_12345", "acme_12345"), ("Filevine_0585-B264", "filevine_0585-b264"),
    ("__hidden.", "hidden"), ("a b/c?d", "a-b-c-d"), ("!!!", "job"), ("x" * 120, "x" * 80),
])
def test_job_slug(raw, expected):
    assert job_slug(raw) == expected


def test_key_layout():
    at = datetime(2026, 10, 3, 9, 30, 15, tzinfo=timezone(timedelta(hours=-3)))
    assert build_upload_key("puma", "Upwork", "acme_12345", 2, "next", at, NOW) == \
        "puma/2026-10-03/upwork/acme_12345/step2_next_2026-10-03_093015.png"
    assert build_upload_key("puma", None, "acme", 1, "step", at, NOW).startswith("puma/2026-10-03/no-profile/acme/")


def test_key_uses_the_bidders_local_date():
    ist = datetime(2026, 10, 4, 0, 5, tzinfo=timezone(timedelta(hours=5, minutes=30)))   # 18:35 UTC on Oct 3
    assert "/2026-10-04/" in build_upload_key("ana", "Backend", "j", 1, "step", ist, NOW)


def test_stale_capture_time_is_rejected():
    with pytest.raises(ValueError, match="capturedAt is not current"):
        build_upload_key("ana", None, "j", 1, "step", NOW - timedelta(hours=25), NOW)
