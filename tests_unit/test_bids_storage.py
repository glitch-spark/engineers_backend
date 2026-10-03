import os
os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

from app.bids.storage import (
    BidStorageError, S3BidStorage, count_folder_windows, count_folders, count_jobs_in_window, get_bid_storage,
    job_first_seen, parse_folder_url,
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
    # Backblaze B2: S3-compatible endpoint, and the web console's "friendly URL"
    ("s3://bid-screenshots/puma/", ("bid-screenshots", "puma/")),
    ("https://s3.us-east-005.backblazeb2.com/bid-screenshots/puma/", ("bid-screenshots", "puma/")),
    ("https://bid-screenshots.s3.us-east-005.backblazeb2.com/puma", ("bid-screenshots", "puma/")),
    ("https://f005.backblazeb2.com/file/bid-screenshots/puma/", ("bid-screenshots", "puma/")),
])
def test_parse_folder_url_accepts(url, expected):
    assert parse_folder_url(url) == expected


@pytest.mark.parametrize("url", ["", "s3://shots", "s3://shots/", "https://example.com/bids/ana/",
                                 "ftp://shots/a/", "not a url",
                                 "https://f005.backblazeb2.com/bid-screenshots/puma/",  # friendly URL needs /file/
                                 "https://f005.backblazeb2.com/file/bid-screenshots/",  # no folder
                                 "https://s3.us-east-005.notbackblazeb2.com/bid-screenshots/puma/"])
def test_parse_folder_url_rejects(url):
    with pytest.raises(ValueError):
        parse_folder_url(url)


JOB = "puma/2026-10-02/upwork"


def test_job_first_seen_groups_screenshots_by_job_folder():
    objs = [
        obj(f"{JOB}/acme/step1_step_2026-10-02_120000.png", utc(2026, 10, 2, 12)),
        obj(f"{JOB}/acme/step2_next_2026-10-02_110000.PNG", utc(2026, 10, 2, 11)),  # earliest wins; upper-case ext
        obj(f"{JOB}/beta/step1_submit_2026-10-02_130000.webp", utc(2026, 10, 2, 13)),
        obj(f"{JOB}/beta/job-info.json", utc(2026, 10, 1)),                         # not an image
        obj(f"{JOB}/_bid_track_connection_test_2026-10-02_090000.png", utc(2026, 10, 2, 9)),  # extension's test upload
    ]
    assert job_first_seen(objs) == {f"{JOB}/acme/": utc(2026, 10, 2, 11), f"{JOB}/beta/": utc(2026, 10, 2, 13)}


def test_jobs_count_in_the_window_of_their_first_screenshot():
    first = {
        "a/": START,                        # start is inclusive
        "b/": END,                          # end is exclusive
        "c/": utc(2026, 10, 1, 23, 29),     # before the window
        "d/": utc(2026, 10, 2),
    }
    assert count_jobs_in_window(first, START, END) == 2


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
    client = FakeClient(pages=[{"Contents": [obj("bids/ana/d/job1/step1.png", utc(2026, 10, 2))]},
                               {"Contents": [obj("bids/ana/d/job2/step1.png", utc(2026, 10, 2))]}, {}])
    assert await S3BidStorage(client=client).count_bids("s3://shots/bids/ana", START, END) == 2
    assert client.calls == [("shots", "bids/ana/")]  # trailing slash: never matches bids/ana2/


async def test_s3_errors_become_bid_storage_error():
    err = ClientError({"Error": {"Code": "AccessDenied", "Message": "nope"}}, "ListObjectsV2")
    with pytest.raises(BidStorageError, match="AccessDenied"):
        await S3BidStorage(client=FakeClient(error=err)).count_bids("s3://shots/a/", START, END)


async def test_bad_url_becomes_bid_storage_error():
    with pytest.raises(BidStorageError, match="invalid folder URL"):
        await S3BidStorage(client=FakeClient()).count_bids("https://example.com/x/", START, END)


async def test_unknown_provider_errors_on_use(monkeypatch):
    monkeypatch.setattr("app.bids.storage.get_settings", lambda: SimpleNamespace(BID_STORAGE_PROVIDER="dropbox"))
    with pytest.raises(BidStorageError, match="unsupported storage provider: dropbox"):
        await get_bid_storage().count_bids("s3://shots/a/", START, END)


async def test_count_folders_keeps_going_after_an_error():
    class Storage:
        async def count_bids(self, url, start, end):
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
        AWS_ACCESS_KEY_ID=None, BID_STORAGE_SECRET_ACCESS_KEY=None, AWS_SECRET_ACCESS_KEY=None,
        BID_STORAGE_ENDPOINT_URL=None))
    storage = S3BidStorage()
    counts = await asyncio.gather(*(storage.count_bids("s3://shots/a/", START, END) for _ in range(5)))
    assert counts == [1] * 5
    assert built == ["s3"]


DAY = (utc(2026, 10, 2, 23, 30), utc(2026, 10, 3, 3))   # open daily window
WEEK = (utc(2026, 9, 26, 21), utc(2026, 10, 3, 3))      # open weekly window


async def test_count_windows_lists_the_folder_once_for_every_window():
    client = FakeClient(pages=[{"Contents": [obj("a/j1/1.png", utc(2026, 10, 3, 1)), obj("a/j2/1.png", utc(2026, 9, 28))]},
                               {"Contents": [obj("a/j3/1.jpg", utc(2026, 10, 2, 23, 30)), obj("a/j4/x.txt", utc(2026, 10, 3))]}])
    assert await S3BidStorage(client=client).count_windows("s3://shots/a/", [DAY, WEEK]) == [2, 3]
    assert client.calls == [("shots", "a/")]


async def test_count_bids_delegates_to_count_windows():
    storage = S3BidStorage(client=FakeClient())
    seen = []

    async def count_windows(url, windows):
        seen.append((url, list(windows)))
        return [6]

    storage.count_windows = count_windows
    assert await storage.count_bids("s3://shots/a/", START, END) == 6
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


async def test_a_job_straddling_the_cutoff_counts_once_in_its_first_window():
    # step1 lands before the 6:30 PM cutoff and step2 after it, on different listing pages
    client = FakeClient(pages=[{"Contents": [obj("a/d/job/step1.png", utc(2026, 10, 2, 23, 0))]},
                               {"Contents": [obj("a/d/job/step2.png", utc(2026, 10, 2, 23, 45))]}])
    today = (utc(2026, 10, 1, 23, 30), utc(2026, 10, 2, 23, 30))
    tomorrow = (utc(2026, 10, 2, 23, 30), utc(2026, 10, 3, 23, 30))
    assert await S3BidStorage(client=client).count_windows("s3://shots/a/", [today, tomorrow]) == [1, 0]


def _client_kwargs(monkeypatch, **settings):
    seen = {}

    class Session:
        def client(self, name, **kwargs):
            seen.update(kwargs)
            return FakeClient()

    base = dict(BID_STORAGE_REGION=None, AWS_REGION="us-east-1", BID_STORAGE_ACCESS_KEY_ID=None,
                AWS_ACCESS_KEY_ID=None, BID_STORAGE_SECRET_ACCESS_KEY=None, AWS_SECRET_ACCESS_KEY=None,
                BID_STORAGE_ENDPOINT_URL=None)
    monkeypatch.setattr("boto3.session.Session", Session)
    monkeypatch.setattr("app.bids.storage.get_settings", lambda: SimpleNamespace(**{**base, **settings}))
    S3BidStorage()._get_client()
    return seen


def test_backblaze_endpoint_uses_path_style_and_bid_credentials(monkeypatch):
    seen = _client_kwargs(monkeypatch, BID_STORAGE_ENDPOINT_URL="https://s3.us-east-005.backblazeb2.com",
                          BID_STORAGE_REGION="us-east-005", BID_STORAGE_ACCESS_KEY_ID="005abc",
                          BID_STORAGE_SECRET_ACCESS_KEY="K005secret")
    assert seen["endpoint_url"] == "https://s3.us-east-005.backblazeb2.com"
    assert seen["region_name"] == "us-east-005"
    assert seen["config"].s3 == {"addressing_style": "path"}
    assert (seen["aws_access_key_id"], seen["aws_secret_access_key"]) == ("005abc", "K005secret")


def test_without_an_endpoint_the_client_targets_aws(monkeypatch):
    seen = _client_kwargs(monkeypatch)
    assert "endpoint_url" not in seen
    assert seen["config"].s3 == {"addressing_style": "virtual"}
