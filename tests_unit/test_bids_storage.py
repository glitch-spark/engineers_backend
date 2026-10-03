import os
os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

from app.bids.storage import (
    BidStorageError, S3BidStorage, count_folder_windows, count_folders, count_in_window, get_bid_storage, parse_folder_url,
)


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


START, END = utc(2026, 10, 1, 23, 30), utc(2026, 10, 2, 23, 30)


def obj(key, when):
    return {"Key": key, "LastModified": when}


@pytest.mark.parametrize("url,expected", [
    ("s3://shots/bids/ana", ("shots", "bids/ana/")),
    ("s3://shots/bids/ana/", ("shots", "bids/ana/")),
    ("  s3://shots/bids/ana/  ", ("shots", "bids/ana/")),
    ("https://shots.s3.us-east-1.amazonaws.com/bids/ana/", ("shots", "bids/ana/")),
    ("https://shots.s3.amazonaws.com/bids/ana", ("shots", "bids/ana/")),
    ("https://s3.us-east-1.amazonaws.com/shots/bids/ana/", ("shots", "bids/ana/")),
    ("s3://shots/bids/ana%20silva/", ("shots", "bids/ana silva/")),
])
def test_parse_folder_url_accepts(url, expected):
    assert parse_folder_url(url) == expected


@pytest.mark.parametrize("url", ["", "s3://shots", "s3://shots/", "https://example.com/bids/ana/",
                                 "ftp://shots/a/", "not a url"])
def test_parse_folder_url_rejects(url):
    with pytest.raises(ValueError):
        parse_folder_url(url)


def test_count_in_window_filters_time_and_extension():
    objs = [
        obj("p/a.png", START),                      # start is inclusive
        obj("p/b.PNG", utc(2026, 10, 2, 12)),       # upper-case extension
        obj("p/c.jpeg", END),                       # end is exclusive
        obj("p/d.jpg", utc(2026, 10, 1, 23, 29)),   # before the window
        obj("p/e.webp", utc(2026, 10, 2)),
        obj("p/notes.txt", utc(2026, 10, 2)),       # not an image
        obj("p/sub/f.jpg", utc(2026, 10, 2)),       # subfolders count
    ]
    assert count_in_window(objs, START, END) == 4


class FakeClient:
    def __init__(self, pages=(), error=None):
        self.pages, self.error, self.calls = list(pages), error, []

    def get_paginator(self, name):
        assert name == "list_objects_v2"
        return SimpleNamespace(paginate=self._paginate)

    def _paginate(self, Bucket, Prefix):
        self.calls.append((Bucket, Prefix))
        if self.error:
            raise self.error
        return iter(self.pages)


async def test_s3_storage_counts_across_pages_with_parsed_prefix():
    client = FakeClient(pages=[{"Contents": [obj("bids/ana/1.png", utc(2026, 10, 2))]},
                               {"Contents": [obj("bids/ana/2.png", utc(2026, 10, 2))]}, {}])
    assert await S3BidStorage(client=client).count_screenshots("s3://shots/bids/ana", START, END) == 2
    assert client.calls == [("shots", "bids/ana/")]  # trailing slash: never matches bids/ana2/


async def test_s3_errors_become_bid_storage_error():
    err = ClientError({"Error": {"Code": "AccessDenied", "Message": "nope"}}, "ListObjectsV2")
    with pytest.raises(BidStorageError, match="AccessDenied"):
        await S3BidStorage(client=FakeClient(error=err)).count_screenshots("s3://shots/a/", START, END)


async def test_bad_url_becomes_bid_storage_error():
    with pytest.raises(BidStorageError, match="invalid folder URL"):
        await S3BidStorage(client=FakeClient()).count_screenshots("https://example.com/x/", START, END)


async def test_unknown_provider_errors_on_use(monkeypatch):
    monkeypatch.setattr("app.bids.storage.get_settings", lambda: SimpleNamespace(BID_STORAGE_PROVIDER="dropbox"))
    with pytest.raises(BidStorageError, match="unsupported storage provider: dropbox"):
        await get_bid_storage().count_screenshots("s3://shots/a/", START, END)


async def test_count_folders_keeps_going_after_an_error():
    class Storage:
        async def count_screenshots(self, url, start, end):
            if "bad" in url:
                raise BidStorageError("NoSuchBucket")
            return 7
    got = await count_folders([("a", "s3://ok/a/"), ("b", "s3://bad/b/")], START, END, Storage())
    assert got == {"a": (7, None), "b": (None, "NoSuchBucket")}


async def test_lazy_client_is_built_once_under_concurrency(monkeypatch):
    import asyncio
    import time

    built = []

    class SlowSession:
        def client(self, name, **kwargs):
            time.sleep(0.05)
            built.append(name)
            return FakeClient(pages=[{"Contents": [obj("a/1.png", utc(2026, 10, 2))]}])

    monkeypatch.setattr("boto3.session.Session", SlowSession)
    monkeypatch.setattr("app.bids.storage.get_settings", lambda: SimpleNamespace(
        BID_STORAGE_REGION=None, AWS_REGION="us-east-1", BID_STORAGE_ACCESS_KEY_ID=None,
        AWS_ACCESS_KEY_ID=None, BID_STORAGE_SECRET_ACCESS_KEY=None, AWS_SECRET_ACCESS_KEY=None))
    storage = S3BidStorage()
    counts = await asyncio.gather(*(storage.count_screenshots("s3://shots/a/", START, END) for _ in range(5)))
    assert counts == [1] * 5
    assert built == ["s3"]


DAY = (utc(2026, 10, 2, 23, 30), utc(2026, 10, 3, 3))   # open daily window
WEEK = (utc(2026, 9, 26, 21), utc(2026, 10, 3, 3))      # open weekly window


async def test_count_windows_lists_the_folder_once_for_every_window():
    client = FakeClient(pages=[{"Contents": [obj("a/1.png", utc(2026, 10, 3, 1)), obj("a/2.png", utc(2026, 9, 28))]},
                               {"Contents": [obj("a/3.jpg", utc(2026, 10, 2, 23, 30)), obj("a/x.txt", utc(2026, 10, 3))]}])
    assert await S3BidStorage(client=client).count_windows("s3://shots/a/", [DAY, WEEK]) == [2, 3]
    assert client.calls == [("shots", "a/")]


async def test_count_screenshots_delegates_to_count_windows():
    storage = S3BidStorage(client=FakeClient())
    seen = []

    async def count_windows(url, windows):
        seen.append((url, list(windows)))
        return [6]

    storage.count_windows = count_windows
    assert await storage.count_screenshots("s3://shots/a/", START, END) == 6
    assert seen == [("s3://shots/a/", [(START, END)])]


async def test_count_windows_errors_become_bid_storage_error(monkeypatch):
    err = ClientError({"Error": {"Code": "NoSuchBucket", "Message": "nope"}}, "ListObjectsV2")
    with pytest.raises(BidStorageError, match="NoSuchBucket"):
        await S3BidStorage(client=FakeClient(error=err)).count_windows("s3://shots/a/", [DAY, WEEK])
    with pytest.raises(BidStorageError, match="invalid folder URL"):
        await S3BidStorage(client=FakeClient()).count_windows("ftp://x/a/", [DAY])
    monkeypatch.setattr("app.bids.storage.get_settings", lambda: SimpleNamespace(BID_STORAGE_PROVIDER="dropbox"))
    with pytest.raises(BidStorageError, match="unsupported storage provider: dropbox"):
        await get_bid_storage().count_windows("s3://shots/a/", [DAY])


async def test_count_folder_windows_isolates_errors_and_bounds_concurrency():
    import asyncio

    running, peak = 0, 0

    class Storage:
        async def count_windows(self, url, windows):
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.01)
            running -= 1
            if "bad" in url:
                raise BidStorageError("AccessDenied")
            return [1, 2][: len(windows)]

    items = [(str(i), f"s3://ok/{i}/") for i in range(8)] + [("x", "s3://bad/x/")]
    got = await count_folder_windows(items, [DAY, WEEK], Storage())
    assert got["0"] == ([1, 2], None) and got["7"] == ([1, 2], None)
    assert got["x"] == (None, "AccessDenied")
    assert peak == 5
