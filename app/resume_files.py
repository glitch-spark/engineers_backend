"""Original files of profile resumes (the uploaded PDF/TXT/MD), kept in the same S3 bucket as generated
resumes. MongoDB keeps only the extracted text plus the S3 key. Without AWS_S3_BUCKET configured nothing is
stored and callers fall back to text-only resumes."""
import asyncio
import logging
from typing import Iterable

from app.config import get_settings
from app.resume_service import _s3_client, generate_presigned_get, s3_enabled

log = logging.getLogger(__name__)

ALLOWED_TYPES = {".pdf": "application/pdf", ".txt": "text/plain", ".md": "text/markdown"}
MAX_FILE_BYTES = 10_000_000


def resume_key(account_id: str, resume_id: str, ext: str) -> str:
    return f"profiles/{account_id}/resumes/{resume_id}{ext}"


async def put_resume_file(key: str, data: bytes, content_type: str) -> bool:
    """Upload one original file; False when S3 isn't configured."""
    if not s3_enabled():
        return False
    bucket = get_settings().AWS_S3_BUCKET
    await asyncio.to_thread(
        _s3_client().put_object,
        Bucket=bucket, Key=key, Body=data, ContentType=content_type, ServerSideEncryption="AES256",
    )
    log.info("uploaded profile resume to s3: bucket=%s key=%s", bucket, key)
    return True


async def delete_resume_files(keys: Iterable[str]) -> None:
    """Best effort: a failed delete leaves an orphan object, never a failed request."""
    objects = [{"Key": k} for k in keys if k]
    if not objects or not s3_enabled():
        return
    try:
        await asyncio.to_thread(
            _s3_client().delete_objects, Bucket=get_settings().AWS_S3_BUCKET, Delete={"Objects": objects, "Quiet": True}
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("could not delete %d profile resume file(s) from s3: %s", len(objects), exc)


class FileUnavailable(Exception):
    """The stored file isn't there (S3 not configured, or no such object)."""


async def read_resume_file(key: str) -> bytes:
    """The stored bytes of one file. Raises FileUnavailable when it isn't there; other S3 errors propagate."""
    if not s3_enabled():
        raise FileUnavailable(key)
    from botocore.exceptions import ClientError

    def get() -> bytes:
        return _s3_client().get_object(Bucket=get_settings().AWS_S3_BUCKET, Key=key)["Body"].read()

    try:
        return await asyncio.to_thread(get)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404", "NotFound"):
            raise FileUnavailable(key) from exc
        raise


def presigned_url(key: str, filename: str) -> str:
    return generate_presigned_get(key, expires=900, download_filename=filename)
