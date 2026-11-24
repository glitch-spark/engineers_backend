"""Original files of profile resumes (the uploaded PDF/TXT/MD), kept in the same S3 bucket as generated
resumes. MongoDB keeps only the extracted text plus the S3 key. Without RESUME_STORAGE_BUCKET configured nothing is
stored and callers fall back to text-only resumes."""
import asyncio
import logging
import base64
from typing import Iterable, Optional

from beanie import PydanticObjectId

from app.config import get_settings
from app.models import Account, ResumeApplication
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
    bucket = get_settings().RESUME_STORAGE_BUCKET
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
            _s3_client().delete_objects, Bucket=get_settings().RESUME_STORAGE_BUCKET, Delete={"Objects": objects, "Quiet": True}
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
        return _s3_client().get_object(Bucket=get_settings().RESUME_STORAGE_BUCKET, Key=key)["Body"].read()

    try:
        return await asyncio.to_thread(get)
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") in ("NoSuchKey", "404", "NotFound"):
            raise FileUnavailable(key) from exc
        raise


def presigned_url(key: str, filename: str) -> str:
    return generate_presigned_get(key, expires=900, download_filename=filename)


async def _stored(key: str) -> bytes:
    try:
        return await read_resume_file(key)
    except FileUnavailable:
        raise FileUnavailable("This resume file isn't available.")


async def read_linked_resume(
    *, kind: str, resume_job_id: Optional[str], account_id: Optional[str], resume_id: Optional[str]
) -> Optional[tuple[str, bytes]]:
    """`(filename, bytes)` of the resume a link points at: "tailored" / "fixed" -> the generated resume record,
    "uploaded" -> the profile's original file. None when nothing is linked. Raises FileUnavailable (its message is
    the reason) when the record or file is gone; other storage errors propagate."""
    if kind in ("tailored", "fixed"):
        if not resume_job_id:
            return None
        doc = await ResumeApplication.get_motor_collection().find_one(
            {"_id": PydanticObjectId(resume_job_id)}, {"s3Key": 1, "pdfData": 1, "companyName": 1}
        )
        if doc is None:
            raise FileUnavailable("This resume no longer exists.")
        if doc.get("s3Key"):
            data = await _stored(doc["s3Key"])
        elif doc.get("pdfData"):
            data = base64.b64decode(doc["pdfData"])
        else:
            raise FileUnavailable("This resume isn't ready.")
        return (f"{doc['companyName']}.pdf" if doc.get("companyName") else "resume.pdf"), data
    if kind == "uploaded" and account_id and resume_id:
        acc = await Account.get(PydanticObjectId(account_id))
        resume = next((r for r in acc.resumes if str(r.id) == resume_id), None) if acc else None
        if resume is None or not resume.s3Key:
            raise FileUnavailable("This resume file isn't available.")
        return resume.filename or "resume.pdf", await _stored(resume.s3Key)
    return None
