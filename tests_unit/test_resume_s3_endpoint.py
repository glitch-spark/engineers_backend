"""Resume storage on an S3-compatible endpoint (Backblaze B2) via AWS_S3_ENDPOINT_URL. Signing is offline."""
import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from types import SimpleNamespace  # noqa: E402
from urllib.parse import urlsplit  # noqa: E402

import pytest  # noqa: E402

from app import resume_service  # noqa: E402


def _settings(endpoint=None, key="005abc", secret="K005secret", region="us-east-005"):
    return SimpleNamespace(AWS_S3_BUCKET="resumes-b2", AWS_REGION=region, AWS_ACCESS_KEY_ID=key,
                           AWS_SECRET_ACCESS_KEY=secret, AWS_S3_ENDPOINT_URL=endpoint)


def test_presigned_url_uses_b2_endpoint_path_style(monkeypatch):
    monkeypatch.setattr(resume_service, "get_settings",
                        lambda: _settings(endpoint="https://s3.us-east-005.backblazeb2.com"))
    url = urlsplit(resume_service.generate_presigned_get("2026_11_05/p/c/Resume.pdf", download_filename="R.pdf"))
    assert url.hostname == "s3.us-east-005.backblazeb2.com"
    assert url.path == "/resumes-b2/2026_11_05/p/c/Resume.pdf"
    assert "response-content-disposition" in url.query


def test_without_endpoint_stays_aws_virtual_host(monkeypatch):
    monkeypatch.setattr(resume_service, "get_settings", lambda: _settings(region="us-east-2"))
    url = urlsplit(resume_service.generate_presigned_get("k/Resume.pdf"))
    assert url.hostname == "resumes-b2.s3.us-east-2.amazonaws.com"
    assert url.path == "/k/Resume.pdf"


def test_settings_without_endpoint_attribute_stay_aws(monkeypatch):
    s = _settings(region="us-east-2")
    del s.AWS_S3_ENDPOINT_URL
    monkeypatch.setattr(resume_service, "get_settings", lambda: s)
    assert "amazonaws.com" in resume_service.generate_presigned_get("k/Resume.pdf")


@pytest.mark.parametrize("key,secret", [(None, "K005secret"), ("005abc", None), (None, None)])
def test_endpoint_without_keys_raises(monkeypatch, key, secret):
    monkeypatch.setattr(resume_service, "get_settings",
                        lambda: _settings(endpoint="https://s3.us-east-005.backblazeb2.com", key=key, secret=secret))
    with pytest.raises(RuntimeError, match="AWS_ACCESS_KEY_ID"):
        resume_service._s3_client()
