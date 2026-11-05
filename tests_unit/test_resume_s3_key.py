"""Generated resumes get a unique S3 key per job (no overwrites for the same profile/company/day)."""
import os

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from types import SimpleNamespace  # noqa: E402

from app import resume_service  # noqa: E402


def test_unique_s3_key(monkeypatch):
    puts = []

    class FakeS3:
        def put_object(self, **kw):
            puts.append(kw["Key"])

    monkeypatch.setattr(resume_service, "get_settings", lambda: SimpleNamespace(RESUME_STORAGE_BUCKET="b", RESUME_STORAGE_REGION="r", RESUME_STORAGE_ENDPOINT_URL=None))
    monkeypatch.setattr(resume_service, "_s3_client", lambda: FakeS3())
    key, _ = resume_service._maybe_upload_to_s3(b"%PDF", "Backend Profile", "Acme Inc", job_id="abc123")
    assert key.endswith("/Backend-Profile/Acme-Inc/abc123/Resume.pdf") or key.endswith("/abc123/Resume.pdf")
    assert puts == [key]
