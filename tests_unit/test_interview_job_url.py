"""Interview jobUrl only accepts http(s) links (it is rendered as a clickable href)."""

import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

import pytest  # noqa: E402
from fastapi import HTTPException  # noqa: E402

from app.routers.interviews import _job_url  # noqa: E402


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, None),
        ("", None),
        ("   ", None),
        ("https://jobs.example.com/123", "https://jobs.example.com/123"),
        ("  http://example.com/a?b=1  ", "http://example.com/a?b=1"),
        ("HTTPS://Example.com/x", "HTTPS://Example.com/x"),
    ],
)
def test_accepts_http_links_and_blank(raw, expected):
    assert _job_url(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "javascript:alert(1)",
        " JavaScript:alert(document.cookie)",
        "java\tscript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "vbscript:msgbox(1)",
        "ftp://example.com/file",
        "http:alert(1)",
        "//example.com/no-scheme",
        "example.com/jobs/1",
        "http://[::1",
    ],
)
def test_rejects_other_schemes(raw):
    with pytest.raises(HTTPException) as exc:
        _job_url(raw)
    assert exc.value.status_code == 400


def test_rejects_overlong():
    with pytest.raises(HTTPException) as exc:
        _job_url("https://example.com/" + "a" * 2100)
    assert exc.value.status_code == 400
