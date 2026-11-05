"""Reading a stored resume file back from S3 (botocore's stubber stands in for AWS: no network)."""
import io
import os
from types import SimpleNamespace

os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

import boto3  # noqa: E402
import pytest  # noqa: E402
from botocore.response import StreamingBody  # noqa: E402
from botocore.stub import Stubber  # noqa: E402

from app import resume_files  # noqa: E402


@pytest.fixture
def s3(monkeypatch):
    client = boto3.client("s3", region_name="us-east-1", aws_access_key_id="x", aws_secret_access_key="x")
    monkeypatch.setattr(resume_files, "_s3_client", lambda: client)
    monkeypatch.setattr(resume_files, "s3_enabled", lambda: True)
    monkeypatch.setattr(resume_files, "get_settings", lambda: SimpleNamespace(RESUME_STORAGE_BUCKET="bucket"))
    with Stubber(client) as stub:
        yield stub


async def test_reads_the_stored_bytes(s3):
    data = b"%PDF-1.7 stored"
    s3.add_response("get_object", {"Body": StreamingBody(io.BytesIO(data), len(data))}, {"Bucket": "bucket", "Key": "k.pdf"})
    assert await resume_files.read_resume_file("k.pdf") == data


async def test_a_missing_object_is_unavailable_not_an_error(s3):
    s3.add_client_error("get_object", service_error_code="NoSuchKey", http_status_code=404)
    with pytest.raises(resume_files.FileUnavailable):
        await resume_files.read_resume_file("gone.pdf")


async def test_other_storage_errors_are_not_hidden(s3):
    from botocore.exceptions import ClientError

    s3.add_client_error("get_object", service_error_code="InternalError", http_status_code=500)
    with pytest.raises(ClientError):  # the route turns this into "try again", not "this file doesn't exist"
        await resume_files.read_resume_file("k.pdf")


async def test_without_storage_nothing_is_available(monkeypatch):
    monkeypatch.setattr(resume_files, "s3_enabled", lambda: False)
    with pytest.raises(resume_files.FileUnavailable):
        await resume_files.read_resume_file("k.pdf")
