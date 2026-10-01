"""Signed, login-free download links for resumes in the exported Google Sheet."""
import os
from datetime import datetime, timedelta, timezone

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

import pytest  # noqa: E402

from app.job_apply import file_links  # noqa: E402


def test_round_trip_and_url():
    token = file_links.make_token("tailored", "job123")
    assert file_links.read_token(token) == {"kind": "tailored", "id": "job123", "accountId": None}
    url = file_links.file_url("resume", "r1", account_id="a1")
    assert "/job-applies/files/" in url
    assert file_links.read_token(url.rsplit("/", 1)[1]) == {"kind": "resume", "id": "r1", "accountId": "a1"}


def test_rejects_tampered_and_expired():
    token = file_links.make_token("resume", "r1", account_id="a1")
    with pytest.raises(file_links.BadLink):
        file_links.read_token(token[:-2] + ("aa" if not token.endswith("aa") else "bb"))
    old = file_links.make_token("resume", "r1", account_id="a1", now=datetime.now(timezone.utc) - timedelta(days=31))
    with pytest.raises(file_links.BadLink):
        file_links.read_token(old)
    with pytest.raises(file_links.BadLink):
        from app.security import create_access_token
        file_links.read_token(create_access_token(sub="u", role="staff"))  # a login token is not a file link
