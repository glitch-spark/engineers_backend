"""Resume storage on an S3-compatible endpoint (Backblaze B2) via RESUME_STORAGE_*. Signing is offline."""
import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from types import SimpleNamespace  # noqa: E402
from urllib.parse import urlsplit  # noqa: E402

import pytest  # noqa: E402

from app import resume_service  # noqa: E402

B2 = "https://s3.us-east-005.backblazeb2.com"


def _settings(endpoint=None, key="005abc", secret="K005secret", region=None, bucket="resumes-b2"):
    return SimpleNamespace(RESUME_STORAGE_BUCKET=bucket, RESUME_STORAGE_REGION=region,
                           RESUME_STORAGE_ACCESS_KEY_ID=key, RESUME_STORAGE_SECRET_ACCESS_KEY=secret,
                           RESUME_STORAGE_ENDPOINT_URL=endpoint,
                           # Must never be used for resumes.
                           AWS_REGION="us-west-1", AWS_ACCESS_KEY_ID="AKIALEAK", AWS_SECRET_ACCESS_KEY="leak")


def _use(monkeypatch, s):
    monkeypatch.setattr(resume_service, "get_settings", lambda: s)


def test_presigned_url_uses_b2_endpoint_path_style(monkeypatch):
    _use(monkeypatch, _settings(endpoint=B2))
    url = urlsplit(resume_service.generate_presigned_get("2026_11_05/p/c/Resume.pdf", download_filename="R.pdf"))
    assert url.hostname == "s3.us-east-005.backblazeb2.com"
    assert url.path == "/resumes-b2/2026_11_05/p/c/Resume.pdf"
    assert "response-content-disposition" in url.query
    assert "005abc" in url.query and "AKIALEAK" not in url.query


def test_region_comes_from_endpoint(monkeypatch):
    _use(monkeypatch, _settings(endpoint=B2))
    assert resume_service.s3_enabled()
    assert resume_service._s3_client().meta.region_name == "us-east-005"


def test_explicit_region_wins(monkeypatch):
    _use(monkeypatch, _settings(endpoint=B2, region="eu-central-003"))
    assert resume_service._s3_client().meta.region_name == "eu-central-003"


def test_without_endpoint_stays_aws_virtual_host(monkeypatch):
    _use(monkeypatch, _settings(region="us-east-2"))
    url = urlsplit(resume_service.generate_presigned_get("k/Resume.pdf"))
    assert url.hostname == "resumes-b2.s3.us-east-2.amazonaws.com"
    assert url.path == "/k/Resume.pdf"


def test_disabled_without_bucket_or_region(monkeypatch):
    _use(monkeypatch, _settings(bucket=None, endpoint=B2))
    assert not resume_service.s3_enabled()
    _use(monkeypatch, _settings())  # AWS without a region
    assert not resume_service.s3_enabled()
    assert resume_service._maybe_upload_to_s3(b"%PDF", "p", "c") == (None, None)


@pytest.mark.parametrize("key,secret", [(None, "K005secret"), ("005abc", None), (None, None)])
def test_endpoint_without_keys_raises(monkeypatch, key, secret):
    _use(monkeypatch, _settings(endpoint=B2, key=key, secret=secret))
    with pytest.raises(RuntimeError, match="RESUME_STORAGE_ACCESS_KEY_ID"):
        resume_service._s3_client()
