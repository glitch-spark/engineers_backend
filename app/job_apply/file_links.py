"""Signed, login-free links to one resume file, for the exported Google Sheet.

A link names one file (a tailored resume = ResumeApplication id, or an uploaded resume = profile + resume id),
is signed with AUTH_SECRET for a separate audience (a login token can't be used as a link), and expires after
LINK_DAYS. Opening it downloads a zip named after the company with the resume inside (see `build_zip`).
"""
import io
import os
import re
import unicodedata
import zipfile
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional
from urllib.parse import quote

from jose import JWTError, jwt

from app.config import get_settings

AUDIENCE = "job-apply-file"
LINK_DAYS = 30
MAX_NAME = 100  # the longest company name a link carries or a zip is named after
Kind = Literal["tailored", "resume"]

_UNSAFE_NAME = re.compile(r"[^\w .()-]+")  # \w is Unicode-aware, so names in any language survive
_UNSAFE_ASCII = re.compile(r"[^A-Za-z0-9 ._()-]+")
_ENTRY_TYPES = (".pdf", ".txt", ".md")  # what a profile can upload (resume_files.ALLOWED_TYPES)


class BadLink(Exception):
    pass


def make_token(
    kind: Kind,
    file_id: str,
    account_id: Optional[str] = None,
    now: Optional[datetime] = None,
    *,
    company: Optional[str] = None,
) -> str:
    """`company` names the zip when the file itself can't say (an uploaded resume); a tailored one knows its own."""
    now = now or datetime.now(timezone.utc)
    payload = {
        "aud": AUDIENCE,
        "k": kind,
        "i": file_id,
        "a": account_id,
        "c": (company or "")[:MAX_NAME] or None,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(days=LINK_DAYS)).timestamp()),
    }
    return jwt.encode(payload, get_settings().AUTH_SECRET, algorithm="HS256")


def read_token(token: str) -> dict:
    try:
        payload = jwt.decode(token, get_settings().AUTH_SECRET, algorithms=["HS256"], audience=AUDIENCE)
    except JWTError as exc:
        raise BadLink(str(exc)) from exc
    if payload.get("k") not in ("tailored", "resume") or not payload.get("i"):
        raise BadLink("not a file link")
    return {"kind": payload["k"], "id": payload["i"], "accountId": payload.get("a"), "company": payload.get("c")}


def file_path(kind: Kind, file_id: str, account_id: Optional[str] = None, *, company: Optional[str] = None) -> str:
    """The link without the API's address, for the app: the browser prefixes the backend it already talks to."""
    return f"/job-applies/files/{make_token(kind, file_id, account_id, company=company)}"


def file_url(kind: Kind, file_id: str, account_id: Optional[str] = None, *, company: Optional[str] = None) -> str:
    """The full link, for places outside the app (the exported sheet): APP_BASE_URL must be the public API address."""
    return get_settings().APP_BASE_URL.rstrip("/") + file_path(kind, file_id, account_id, company=company)


# ---------- the download: <Company>.zip holding the resume ----------


def zip_name(company: Optional[str]) -> str:
    """The zip's file name: the company, made safe for a file system ("Acme, Inc." -> "Acme_ Inc.zip")."""
    name = _UNSAFE_NAME.sub("_", company or "")[:MAX_NAME].strip(" .")  # Windows drops trailing spaces and dots
    return f"{name or 'Resume'}.zip"


def entry_name(original_filename: Optional[str]) -> str:
    """The resume's name inside the zip: always "resume", keeping its type (a profile can upload .pdf, .txt, .md)."""
    ext = os.path.splitext(original_filename or "")[1].lower()
    return f"resume{ext if ext in _ENTRY_TYPES else '.pdf'}"


def build_zip(data: bytes, name: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(name, data)
    return buf.getvalue()


def content_disposition(filename: str) -> str:
    """A download header that survives any name: a plain-ASCII fallback, plus the real name (RFC 5987) for browsers."""
    stem, ext = os.path.splitext(filename)
    ascii_stem = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode()
    ascii_stem = _UNSAFE_ASCII.sub("_", ascii_stem).strip(" .") or "download"
    return f"attachment; filename=\"{ascii_stem}{ext}\"; filename*=UTF-8''{quote(filename, safe='')}"
