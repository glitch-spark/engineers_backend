"""Storage adapter: counts bidders' screenshots in a time window.

Only S3 is implemented; `get_bid_storage()` picks the provider from settings.
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime
from typing import Iterable, Mapping, Optional, Protocol, Sequence
from urllib.parse import unquote, urlsplit

from botocore.exceptions import BotoCoreError, ClientError

from app.config import get_settings

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp")

# bucket.s3.amazonaws.com / bucket.s3.<region>.amazonaws.com (bucket names may contain dots)
_VIRTUAL_HOST = re.compile(r"^(?P<bucket>.+)\.s3[.-](?:[a-z0-9-]+\.)?amazonaws\.com$")
# s3.amazonaws.com / s3.<region>.amazonaws.com / s3-<region>.amazonaws.com
_PATH_HOST = re.compile(r"^s3[.-](?:[a-z0-9-]+\.)?amazonaws\.com$")


class BidStorageError(Exception):
    """Short message, shown to users."""


class BidStorage(Protocol):
    async def count_screenshots(self, folder_url: str, start: datetime, end: datetime) -> int: ...


def parse_folder_url(url: str) -> tuple[str, str]:
    """Return (bucket, prefix ending in '/') for an S3 folder URL, else ValueError."""
    parts = urlsplit((url or "").strip())
    host = (parts.hostname or "").lower()
    path = unquote(parts.path).lstrip("/")
    if parts.scheme == "s3":
        bucket, prefix = host, path
    elif parts.scheme == "https" and _PATH_HOST.match(host):
        bucket, _, prefix = path.partition("/")
    elif parts.scheme == "https" and (m := _VIRTUAL_HOST.match(host)):
        bucket, prefix = m.group("bucket"), path
    else:
        raise ValueError(f"unrecognised folder URL: {url!r}")
    prefix = prefix.strip("/")
    if not bucket or not prefix:
        raise ValueError(f"folder URL needs a bucket and a folder: {url!r}")
    return bucket, prefix + "/"


def count_in_window(objects: Iterable[Mapping], start: datetime, end: datetime) -> int:
    """Count image objects with start <= LastModified < end (subfolders included)."""
    return sum(
        1
        for o in objects
        if o["Key"].lower().endswith(IMAGE_EXTENSIONS) and start <= o["LastModified"] < end
    )


class S3BidStorage:
    def __init__(self, client=None):
        self._client = client

    def _get_client(self):
        if self._client is None:
            import boto3
            from botocore.config import Config

            s = get_settings()
            kwargs = {
                "region_name": s.BID_STORAGE_REGION or s.AWS_REGION,
                "config": Config(signature_version="s3v4", s3={"addressing_style": "virtual"}),
            }
            key = s.BID_STORAGE_ACCESS_KEY_ID or s.AWS_ACCESS_KEY_ID
            secret = s.BID_STORAGE_SECRET_ACCESS_KEY or s.AWS_SECRET_ACCESS_KEY
            if key and secret:
                kwargs["aws_access_key_id"] = key
                kwargs["aws_secret_access_key"] = secret
            self._client = boto3.client("s3", **kwargs)
        return self._client

    def _count(self, bucket: str, prefix: str, start: datetime, end: datetime) -> int:
        # Pages are fetched lazily, so the whole loop runs inside one worker thread.
        total = 0
        for page in self._get_client().get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
            total += count_in_window(page.get("Contents", []), start, end)
        return total

    async def count_screenshots(self, folder_url: str, start: datetime, end: datetime) -> int:
        try:
            bucket, prefix = parse_folder_url(folder_url)
        except ValueError:
            raise BidStorageError("invalid folder URL") from None
        try:
            return await asyncio.to_thread(self._count, bucket, prefix, start, end)
        except ClientError as e:
            raise BidStorageError(e.response.get("Error", {}).get("Code") or "ClientError") from e
        except BotoCoreError as e:
            raise BidStorageError(type(e).__name__) from e


class _UnsupportedStorage:
    def __init__(self, provider: str):
        self._provider = provider

    async def count_screenshots(self, folder_url: str, start: datetime, end: datetime) -> int:
        raise BidStorageError(f"unsupported storage provider: {self._provider}")


def get_bid_storage() -> BidStorage:
    provider = get_settings().BID_STORAGE_PROVIDER
    if provider == "s3":
        return S3BidStorage()
    return _UnsupportedStorage(provider)


async def count_folders(
    items: Sequence[tuple[str, str]],
    start: datetime,
    end: datetime,
    storage: BidStorage,
    limit: int = 5,
) -> dict[str, tuple[Optional[int], Optional[str]]]:
    """Count each (id, folder_url) with bounded concurrency; one failing folder doesn't stop the rest."""
    sem = asyncio.Semaphore(limit)

    async def one(folder_url: str) -> tuple[Optional[int], Optional[str]]:
        async with sem:
            try:
                return await storage.count_screenshots(folder_url, start, end), None
            except BidStorageError as e:
                return None, str(e)

    results = await asyncio.gather(*(one(url) for _, url in items))
    return {item_id: res for (item_id, _), res in zip(items, results)}
