"""Storage adapter: counts bidders' bids in a time window.

The Bid Track extension uploads one or more screenshots per job application, under
`<member>/<YYYY-MM-DD>/<profile>/<job>/step<n>_<trigger>_<stamp>.png`. A bid is one job
folder, counted in the window of its earliest screenshot.

Only S3 is implemented (AWS, or an S3-compatible endpoint such as Backblaze B2);
`get_bid_storage()` picks the provider from settings.
"""

from __future__ import annotations

import asyncio
import re
import threading
from datetime import datetime
from typing import Awaitable, Callable, Iterable, Mapping, Optional, Protocol, Sequence, TypeVar
from urllib.parse import unquote, urlsplit

from botocore.exceptions import BotoCoreError, ClientError

from app.config import get_settings

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp")

# bucket.s3.amazonaws.com / bucket.s3.<region>.amazonaws.com (bucket names may contain dots),
# bucket.s3.<region>.backblazeb2.com
_VIRTUAL_HOST = re.compile(
    r"^(?P<bucket>.+)\.s3[.-](?:[a-z0-9-]+\.)?amazonaws\.com$|^(?P<b2bucket>.+)\.s3\.[a-z0-9-]+\.backblazeb2\.com$"
)
# s3.amazonaws.com / s3.<region>.amazonaws.com / s3-<region>.amazonaws.com / s3.<region>.backblazeb2.com
_PATH_HOST = re.compile(r"^s3[.-](?:[a-z0-9-]+\.)?amazonaws\.com$|^s3\.[a-z0-9-]+\.backblazeb2\.com$")
# Backblaze web console "friendly URL": https://f005.backblazeb2.com/file/<bucket>/<prefix>
_B2_FRIENDLY_HOST = re.compile(r"^f\d+\.backblazeb2\.com$")


class BidStorageError(Exception):
    """Short message, shown to users."""


Window = tuple[datetime, datetime]


class BidStorage(Protocol):
    async def count_bids(self, folder_url: str, start: datetime, end: datetime) -> int: ...

    async def count_windows(self, folder_url: str, windows: Sequence[Window]) -> list[int]:
        """Bid counts for several windows from one folder listing (S3 cannot filter by date)."""
        ...


def parse_folder_url(url: str) -> tuple[str, str]:
    """Return (bucket, prefix ending in '/') for an S3 or Backblaze folder URL, else ValueError."""
    parts = urlsplit((url or "").strip())
    host = (parts.hostname or "").lower()
    path = unquote(parts.path).lstrip("/")
    if parts.scheme == "s3":
        bucket, prefix = host, path
    elif parts.scheme == "https" and _PATH_HOST.match(host):
        bucket, _, prefix = path.partition("/")
    elif parts.scheme == "https" and (m := _VIRTUAL_HOST.match(host)):
        bucket, prefix = m.group("bucket") or m.group("b2bucket"), path
    elif parts.scheme == "https" and _B2_FRIENDLY_HOST.match(host) and path.startswith("file/"):
        bucket, _, prefix = path.removeprefix("file/").partition("/")
    else:
        raise ValueError(f"unrecognised folder URL: {url!r}")
    prefix = prefix.strip("/")
    if not bucket or not prefix:
        raise ValueError(f"folder URL needs a bucket and a folder: {url!r}")
    if prefix.lower().endswith(IMAGE_EXTENSIONS):
        raise ValueError(f"folder URL points at a file, not the bidder's folder: {url!r}")
    return bucket, prefix + "/"


def _endpoint_region(endpoint: str) -> Optional[str]:
    """Region from an S3-compatible endpoint host, e.g. s3.us-east-005.backblazeb2.com -> us-east-005."""
    m = re.match(r"^s3[.-]([a-z0-9-]+)\.", (urlsplit(endpoint).hostname or "").lower())
    return m.group(1) if m else None


def job_first_seen(objects: Iterable[Mapping], into: Optional[dict[str, datetime]] = None) -> dict[str, datetime]:
    """Map each job folder (the folder holding a screenshot) to its earliest image upload time.

    Pass `into` to accumulate across listing pages.
    """
    first = {} if into is None else into
    for o in objects:  # "_"-prefixed files (connection tests) are not bids
        folder, _, name = o["Key"].rpartition("/")
        if not name.lower().endswith(IMAGE_EXTENSIONS) or name.startswith("_"):
            continue
        job, when = folder + "/", o["LastModified"]
        if job not in first or when < first[job]:
            first[job] = when
    return first


def count_jobs_in_window(first_seen: Mapping[str, datetime], start: datetime, end: datetime) -> int:
    """Count jobs whose first screenshot was uploaded in start <= t < end."""
    return sum(1 for when in first_seen.values() if start <= when < end)


class S3BidStorage:
    def __init__(self, client=None):
        self._client = client
        self._lock = threading.Lock()  # count_folders runs _get_client from several threads

    def _get_client(self):
        with self._lock:
            if self._client is None:
                import boto3
                from botocore.config import Config

                s = get_settings()
                endpoint = s.BID_STORAGE_ENDPOINT_URL
                if endpoint:
                    # A non-AWS store must never be sent the AWS_* region or keys.
                    region = s.BID_STORAGE_REGION or _endpoint_region(endpoint)
                    key, secret = s.BID_STORAGE_ACCESS_KEY_ID, s.BID_STORAGE_SECRET_ACCESS_KEY
                else:
                    region = s.BID_STORAGE_REGION or s.AWS_REGION
                    key = s.BID_STORAGE_ACCESS_KEY_ID or s.AWS_ACCESS_KEY_ID
                    secret = s.BID_STORAGE_SECRET_ACCESS_KEY or s.AWS_SECRET_ACCESS_KEY
                kwargs = {
                    "region_name": region,
                    # Path style for custom endpoints (Backblaze): no per-bucket DNS or TLS name needed.
                    "config": Config(signature_version="s3v4",
                                     s3={"addressing_style": "path" if endpoint else "virtual"}),
                }
                if endpoint:
                    kwargs["endpoint_url"] = endpoint
                if key and secret:
                    kwargs["aws_access_key_id"] = key
                    kwargs["aws_secret_access_key"] = secret
                try:
                    # Own session: the shared default session is not thread-safe.
                    self._client = boto3.session.Session().client("s3", **kwargs)
                except ValueError:  # botocore: "Invalid endpoint: ..."
                    raise BidStorageError("invalid BID_STORAGE_ENDPOINT_URL") from None
            return self._client

    def presign_put(self, bucket: str, key: str, content_type: str = "image/png", expires: int = 300) -> str:
        """Short-lived pre-signed PUT link; signing is offline (no network call)."""
        try:
            return self._get_client().generate_presigned_url(
                "put_object", Params={"Bucket": bucket, "Key": key, "ContentType": content_type},
                ExpiresIn=expires, HttpMethod="PUT",
            )
        except ValueError:
            raise BidStorageError("invalid BID_STORAGE_ENDPOINT_URL") from None

    def _count(self, bucket: str, prefix: str, windows: Sequence[Window]) -> list[int]:
        # Pages are fetched lazily, so the whole loop runs inside one worker thread. A job's
        # screenshots can sit on different pages, so first-seen times are gathered before counting.
        first: dict[str, datetime] = {}
        for page in self._get_client().get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
            job_first_seen(page.get("Contents", []), into=first)
        return [count_jobs_in_window(first, start, end) for start, end in windows]

    async def count_bids(self, folder_url: str, start: datetime, end: datetime) -> int:
        return (await self.count_windows(folder_url, [(start, end)]))[0]

    async def count_windows(self, folder_url: str, windows: Sequence[Window]) -> list[int]:
        try:
            bucket, prefix = parse_folder_url(folder_url)
        except ValueError:
            raise BidStorageError("invalid folder URL") from None
        try:
            return await asyncio.to_thread(self._count, bucket, prefix, list(windows))
        except ClientError as e:
            raise BidStorageError(e.response.get("Error", {}).get("Code") or "ClientError") from e
        except BotoCoreError as e:
            raise BidStorageError(type(e).__name__) from e


class _UnsupportedStorage:
    def __init__(self, provider: str):
        self._provider = provider

    async def count_bids(self, folder_url: str, start: datetime, end: datetime) -> int:
        return (await self.count_windows(folder_url, [(start, end)]))[0]

    async def count_windows(self, folder_url: str, windows: Sequence[Window]) -> list[int]:
        raise BidStorageError(f"unsupported storage provider: {self._provider}")


def get_bid_storage() -> BidStorage:
    provider = get_settings().BID_STORAGE_PROVIDER
    if provider == "s3":
        return S3BidStorage()
    return _UnsupportedStorage(provider)


T = TypeVar("T")


async def _each_folder(
    items: Sequence[tuple[str, str]], count: Callable[[str], Awaitable[T]], limit: int,
) -> dict[str, tuple[Optional[T], Optional[str]]]:
    """Run count(folder_url) per (id, folder_url), bounded; one failing folder doesn't stop the rest."""
    sem = asyncio.Semaphore(limit)

    async def one(folder_url: str) -> tuple[Optional[T], Optional[str]]:
        async with sem:
            try:
                return await count(folder_url), None
            except BidStorageError as e:
                return None, str(e)

    results = await asyncio.gather(*(one(url) for _, url in items))
    return {item_id: res for (item_id, _), res in zip(items, results)}


async def count_folders(
    items: Sequence[tuple[str, str]],
    start: datetime,
    end: datetime,
    storage: BidStorage,
    limit: int = 5,
) -> dict[str, tuple[Optional[int], Optional[str]]]:
    """Count each (id, folder_url) in one window -> {id: (count, error)}."""
    return await _each_folder(items, lambda url: storage.count_bids(url, start, end), limit)


async def count_folder_windows(
    items: Sequence[tuple[str, str]],
    windows: Sequence[Window],
    storage: BidStorage,
    limit: int = 5,
) -> dict[str, tuple[Optional[list[int]], Optional[str]]]:
    """Count each (id, folder_url) in every window from one listing -> {id: (counts per window, error)}."""
    return await _each_folder(items, lambda url: storage.count_windows(url, windows), limit)
