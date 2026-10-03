# Bidders and Bid Reports Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Users manage their bidders, and the cron posts daily bid counts and weekly bid counts with pay to a shared Slack channel, counted from each bidder's screenshot folder.

**Architecture:** Pure, unit-tested core modules in a new `app/bids/` package (schedule windows, storage adapter, Slack formatting, report tick, live counts). Beanie models hold bidders and saved report rows. A thin FastAPI router exposes CRUD and history. The report tick runs inside the existing 5-minute Railway cron (`app/alerts_tick.py`). The frontend gets one new page with a form modal and a history side panel.

**Tech Stack:** FastAPI, Beanie/Motor, pydantic-settings, boto3, httpx (Slack Web API), pytest + pytest-asyncio; React 18 + TypeScript + Vite + Tailwind + SWR.

**Spec:** `engineers_backend/docs/superpowers/specs/2026-10-02-bidder-management-design.md`

**Repos and branches:** backend tasks 1–6 in `engineers_backend` (branch `feat/bid-manage`, from `dev`); frontend tasks 7–8 in `engineers_app` (branch `feat/bid-manage`, from `main`). Paths below are relative to the repo named in each task.

**Test command (backend):** `.venv/Scripts/python.exe -m pytest tests_unit/<file> -v` from `engineers_backend`. Unit tests never touch MongoDB or the network: every test file starts with
```python
import os
os.environ.setdefault("MONGODB_URI", "mongodb://localhost:27017/unit-test")
os.environ.setdefault("AUTH_SECRET", "unit-test-secret")
```
before importing `app.*`. Beanie `Document` subclasses cannot be instantiated without a database (`CollectionWasNotInitialized`), so the core logic works on plain pydantic rows (`BidReportRow`) and duck-typed bidders (`SimpleNamespace` in tests).

## Global Constraints

- Report timezone default `America/Chicago`; daily cutoff `18:30`; weekly cutoff `saturday` `16:00`; all four come from `.env`.
- Windows are `[start, end)`, built as local wall-clock times and then converted to UTC.
- Catch-up window: a report runs only if `now − cutoff < 6 h`.
- A bid is one key ending in `.png .jpg .jpeg .webp` (case-insensitive) with `LastModified` in the window. Subfolders count.
- Rate: USD per bid, `0 ≤ rate ≤ 1000`, at most 2 decimals. `amount = round(count × rate, 2)`.
- Slack de-dup keys: `bids-daily:<periodKey>` and `bids-weekly:<periodKey>` in `SlackAlertLog` with `userId = CALLER_CHANNEL_SENTINEL` (`app/slack_bot.py`).
- Bidder access is owner-only (`createdBy == user.id`), otherwise **404**. Profile choices: own non-archived accounts; admins any non-archived account.
- Concurrency limit for storage counts: 5. Live-count cache TTL: 5 minutes.
- Slack text limit per section block: 3,000 characters. Blocks per message: 50.
- MongoDB keys stay camelCase (existing convention). Collections: `bidders`, `bidreports`.

## Review Focus

1. A folder URL pasted with spaces around it, without a trailing slash, or with `%20` in it should still resolve to the right prefix. Covered in Task 2 (`test_parse_folder_url_accepts`).
2. Screenshots saved as `.PNG`/`.JPG` (upper-case extensions, common from Windows tools) must count. Covered in Task 2 (`test_count_in_window_filters_time_and_extension`).
3. A screenshot uploaded at exactly 6:30:00 PM belongs to the *next* day, never to both. Covered in Task 2 (same test: start inclusive, end exclusive).
4. Rates like `0.1` or `19.99` must not show float noise (`$0.30000000000000004`) in Slack or the page. Covered in Task 3 (`test_two_decimal_rates_accepted`) and Task 5 (`test_amount_is_rounded_rate_snapshot`).
5. A bidder whose owner account was deleted must still appear in the report, under "Unknown owner", and must not crash the post. Covered in Task 4 (`test_missing_owner_falls_back`).

---

### Task 1: Report schedule settings and windows

**Files:**
- Modify: `app/config.py` (add settings + validators after the `AWS_*` block)
- Modify: `.env.example` (new `# Bidders` block after the AWS block, the 9 keys from spec §4 with comments)
- Create: `app/bids/__init__.py` (empty), `app/bids/windows.py`
- Test: `tests_unit/test_bids_windows.py`

**Interfaces:**
- Produces:
  - Settings fields: `BID_STORAGE_PROVIDER: str = "s3"`, `BID_STORAGE_ACCESS_KEY_ID / BID_STORAGE_SECRET_ACCESS_KEY / BID_STORAGE_REGION: Optional[str] = None`, `BID_REPORT_TIMEZONE: str = "America/Chicago"`, `BID_DAILY_REPORT_TIME: str = "18:30"`, `BID_WEEKLY_REPORT_DAY: str = "saturday"`, `BID_WEEKLY_REPORT_TIME: str = "16:00"`, `SLACK_BIDS_CHANNEL_ID: Optional[str] = None`.
  - `Kind = Literal["daily", "weekly"]`
  - `@dataclass(frozen=True) class BidSchedule: tz: ZoneInfo; daily_at: time; weekly_day: int  # Monday=0; weekly_at: time`
  - `schedule_from_settings(s: Settings | None = None) -> BidSchedule`
  - `latest_cutoff(kind: Kind, now: datetime, sched: BidSchedule) -> datetime` (aware, in `sched.tz`, `<= now`)
  - `window_for(kind: Kind, cutoff: datetime, sched: BidSchedule) -> tuple[datetime, datetime]` (UTC-aware `[start, end)`)
  - `open_window(kind: Kind, now: datetime, sched: BidSchedule) -> tuple[datetime, datetime]` (`[latest_cutoff in UTC, now]`)
  - `period_key(cutoff: datetime) -> str` (`YYYY-MM-DD` of the cutoff's local date)

- [ ] **Step 1: Write the failing tests**

```python
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from app.bids.windows import BidSchedule, latest_cutoff, open_window, period_key, schedule_from_settings, window_for
from app.config import Settings

CT = ZoneInfo("America/Chicago")
SCHED = BidSchedule(tz=CT, daily_at=time(18, 30), weekly_day=5, weekly_at=time(16, 0))


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


def test_daily_cutoff_before_and_after_the_time():
    # Fri 2026-10-02 18:30 CDT == 23:30 UTC
    assert latest_cutoff("daily", utc(2026, 10, 2, 23, 29), SCHED) == datetime(2026, 10, 1, 18, 30, tzinfo=CT)
    assert latest_cutoff("daily", utc(2026, 10, 2, 23, 30), SCHED) == datetime(2026, 10, 2, 18, 30, tzinfo=CT)


def test_daily_window_is_one_local_day_in_utc():
    assert window_for("daily", datetime(2026, 10, 2, 18, 30, tzinfo=CT), SCHED) == (
        utc(2026, 10, 1, 23, 30), utc(2026, 10, 2, 23, 30))


def test_daily_window_across_dst_end_is_25_hours():
    start, end = window_for("daily", datetime(2026, 11, 1, 18, 30, tzinfo=CT), SCHED)  # DST ends Sun Nov 1
    assert end - start == timedelta(hours=25)
    assert end == utc(2026, 11, 2, 0, 30)


def test_daily_window_across_dst_start_is_23_hours():
    start, end = window_for("daily", datetime(2026, 3, 8, 18, 30, tzinfo=CT), SCHED)  # DST starts Sun Mar 8
    assert end - start == timedelta(hours=23)


def test_weekly_cutoff_is_most_recent_saturday_4pm():
    # Sat 2026-10-03 16:00 CDT == 21:00 UTC
    assert latest_cutoff("weekly", utc(2026, 10, 3, 20, 59), SCHED) == datetime(2026, 9, 26, 16, 0, tzinfo=CT)
    assert latest_cutoff("weekly", utc(2026, 10, 3, 21, 0), SCHED) == datetime(2026, 10, 3, 16, 0, tzinfo=CT)
    assert latest_cutoff("weekly", utc(2026, 10, 7, 12, 0), SCHED) == datetime(2026, 10, 3, 16, 0, tzinfo=CT)


def test_weekly_window_over_dst_end_is_169_hours():
    start, end = window_for("weekly", datetime(2026, 11, 7, 16, 0, tzinfo=CT), SCHED)
    assert end - start == timedelta(hours=169)


def test_open_window_runs_from_latest_cutoff_to_now():
    now = utc(2026, 10, 3, 3, 0)
    assert open_window("daily", now, SCHED) == (utc(2026, 10, 2, 23, 30), now)


def test_period_key_is_local_date_of_cutoff():
    assert period_key(datetime(2026, 10, 2, 18, 30, tzinfo=CT)) == "2026-10-02"


def test_schedule_from_settings_parses_values():
    s = Settings(_env_file=None, MONGODB_URI="m", AUTH_SECRET="a", BID_REPORT_TIMEZONE="UTC",
                 BID_DAILY_REPORT_TIME="07:05", BID_WEEKLY_REPORT_DAY="Monday", BID_WEEKLY_REPORT_TIME="09:00")
    assert schedule_from_settings(s) == BidSchedule(tz=ZoneInfo("UTC"), daily_at=time(7, 5), weekly_day=0,
                                                    weekly_at=time(9, 0))


@pytest.mark.parametrize("field,value", [
    ("BID_DAILY_REPORT_TIME", "25:00"), ("BID_DAILY_REPORT_TIME", "6:30pm"), ("BID_WEEKLY_REPORT_TIME", "16"),
    ("BID_WEEKLY_REPORT_DAY", "funday"), ("BID_REPORT_TIMEZONE", "Mars/Base"),
])
def test_invalid_schedule_settings_fail_validation(field, value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, MONGODB_URI="m", AUTH_SECRET="a", **{field: value})
```

- [ ] **Step 2: Run to verify failure** — `pytest tests_unit/test_bids_windows.py -v` → FAIL, `ModuleNotFoundError: app.bids`.

- [ ] **Step 3: Add the settings and validators to `app/config.py`**

`field_validator`s: times must match `^([01]\d|2[0-3]):[0-5]\d$`; the day is lower-cased and must be one of `monday … sunday`; the timezone must load with `ZoneInfo`. Validators return the normalized string. Comment each field the way neighbouring fields are commented.

- [ ] **Step 4: Implement `app/bids/windows.py`** with the Interfaces above. Build every boundary with `datetime.combine(local_date, at, tzinfo=sched.tz)`, never by subtracting hours from an aware datetime. That is what makes DST days 23 or 25 hours long. Weekly: go back `(local.weekday() − weekly_day) % 7` days; if that candidate is after `now`, go back 7 more local days.

- [ ] **Step 5: Run to verify pass** — same command → all PASS.

- [ ] **Step 6: Commit**

```bash
git add app/config.py .env.example app/bids/__init__.py app/bids/windows.py tests_unit/test_bids_windows.py
git commit -m "feat(bids): report schedule settings and DST-safe windows"
```

---

### Task 2: Storage adapter

**Files:**
- Create: `app/bids/storage.py`
- Test: `tests_unit/test_bids_storage.py`

**Interfaces:**
- Consumes: settings fields from Task 1.
- Produces:
  - `IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp")`
  - `class BidStorageError(Exception)` (message is short and shown to users)
  - `class BidStorage(Protocol): async def count_screenshots(self, folder_url: str, start: datetime, end: datetime) -> int`
  - `parse_folder_url(url: str) -> tuple[str, str]` (bucket, prefix ending in `/`), `ValueError` otherwise
  - `count_in_window(objects: Iterable[Mapping], start: datetime, end: datetime) -> int` (pure; items have `Key`, `LastModified`)
  - `class S3BidStorage: __init__(self, client=None)` (client built lazily from `BID_STORAGE_*` falling back to `AWS_*`)
  - `get_bid_storage() -> BidStorage`
  - `async def count_folders(items: Sequence[tuple[str, str]], start: datetime, end: datetime, storage: BidStorage, limit: int = 5) -> dict[str, tuple[Optional[int], Optional[str]]]` (input is `(id, folder_url)` pairs; output maps id → `(count, None)` or `(None, error message)`)

- [ ] **Step 1: Write the failing tests**

```python
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from botocore.exceptions import ClientError

from app.bids.storage import BidStorageError, S3BidStorage, count_folders, count_in_window, get_bid_storage, parse_folder_url


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
```

- [ ] **Step 2: Run to verify failure** — `pytest tests_unit/test_bids_storage.py -v` → FAIL, import error.

- [ ] **Step 3: Implement `app/bids/storage.py`.**
  - `parse_folder_url`: strip, then `urllib.parse.urlsplit`. Recognise the three forms in spec §3. Hostnames must end in `amazonaws.com`. `unquote` the prefix, strip the leading `/`, append `/`. Reject an empty bucket or prefix.
  - `S3BidStorage.count_screenshots`: `parse_folder_url` (a `ValueError` becomes `BidStorageError("invalid folder URL")`), then run the whole paginate-and-count loop in one `asyncio.to_thread` call, because pages raise lazily. Map `ClientError` to its `Error.Code`, and `BotoCoreError` to `type(e).__name__`.
  - Client construction mirrors `_s3_client()` in `app/resume_service.py` (s3v4, virtual addressing). The region and keys come from `BID_STORAGE_*` or the `AWS_*` fallbacks.
  - An unknown provider returns a stub whose `count_screenshots` raises `BidStorageError(f"unsupported storage provider: {name}")`.
  - `count_folders` uses `asyncio.Semaphore(limit)` and `asyncio.gather`. Only `BidStorageError` is caught per item. Anything else propagates.

- [ ] **Step 4: Run to verify pass** — same command → all PASS.

- [ ] **Step 5: Commit**

```bash
git add app/bids/storage.py tests_unit/test_bids_storage.py
git commit -m "feat(bids): S3 screenshot-count storage adapter"
```

---

### Task 3: Models and request schema

**Files:**
- Create: `app/models/bidder.py`, `app/models/bid_report.py`, `app/bids/schemas.py`
- Modify: `app/models/__init__.py` (export `Bidder`, `BidReport`), `app/db.py` (append both to `DOCUMENT_MODELS`)
- Test: `tests_unit/test_bids_schemas.py`

**Interfaces:**
- Consumes: `parse_folder_url` (Task 2).
- Produces:
  - `class Bidder(TimestampedDocument)`: fields from spec §2. `Settings.name = "bidders"`, index `[("createdBy", 1), ("archivedAt", 1)]`.
  - `class BidReportRow(BaseModel)`: `kind: Kind`, `periodKey: str`, `periodStart: datetime`, `periodEnd: datetime`, `bidderId: PydanticObjectId`, `ownerId: PydanticObjectId`, `bidderName: str`, `country: Optional[str] = None`, `profileName: Optional[str] = None`, `rate: float`, `count: Optional[int] = None`, `amount: Optional[float] = None`, `error: Optional[str] = None`.
  - `class BidReport(TimestampedDocument, BidReportRow)`: `Settings.name = "bidreports"`. Indexes: unique `[("kind",1),("periodKey",1),("bidderId",1)]`, and `[("bidderId",1),("kind",1),("periodKey",-1)]`.
  - `class BidderIn(BaseModel)`: `name: str`, `country: Optional[str] = None`, `profileId: Optional[str] = None`, `rate: float`, `screenshotFolderUrl: str`.
  - `bidder_out(b, profile_name: Optional[str]) -> dict` with keys `_id, name, country, profileId, profileName, rate, screenshotFolderUrl, archivedAt, createdAt` (ids as `str`, datetimes as ISO).
  - `report_out(r) -> dict` with keys `kind, periodKey, periodStart, periodEnd, count, rate, amount, error`.

- [ ] **Step 1: Write the failing tests**

```python
import pytest
from beanie import PydanticObjectId
from pydantic import ValidationError

from app.bids.schemas import BidderIn
from app.models.bid_report import BidReport
from app.models.bidder import Bidder


def valid(**over):
    base = dict(name=" Ana Silva ", country="br", profileId=str(PydanticObjectId()), rate=0.5,
                screenshotFolderUrl=" s3://shots/bids/ana/ ")
    base.update(over)
    return base


def test_bidder_in_normalizes():
    b = BidderIn(**valid())
    assert (b.name, b.country, b.rate, b.screenshotFolderUrl) == ("Ana Silva", "BR", 0.5, "s3://shots/bids/ana/")


def test_blank_optional_fields_become_none():
    b = BidderIn(**valid(country="", profileId=""))
    assert b.country is None and b.profileId is None


@pytest.mark.parametrize("field,value", [
    ("name", "   "), ("name", "x" * 101), ("country", "BRA"), ("country", "1A"),
    ("rate", -0.01), ("rate", 1000.01), ("rate", 0.505), ("profileId", "nope"),
    ("screenshotFolderUrl", "https://example.com/a/"), ("screenshotFolderUrl", "s3://shots"),
])
def test_bidder_in_rejects(field, value):
    with pytest.raises(ValidationError) as exc:
        BidderIn(**valid(**{field: value}))
    assert exc.value.errors()[0]["loc"] == (field,)


@pytest.mark.parametrize("rate", [0, 0.1, 0.29, 19.99, 1000])
def test_two_decimal_rates_accepted(rate):
    assert BidderIn(**valid(rate=rate)).rate == rate


def test_collections_and_unique_report_index():
    assert Bidder.Settings.name == "bidders" and BidReport.Settings.name == "bidreports"
    assert Bidder.model_fields["archivedAt"].default is None
    unique = [ix.document for ix in BidReport.Settings.indexes if ix.document.get("unique")]
    assert [list(ix["key"].items()) for ix in unique] == [[("kind", 1), ("periodKey", 1), ("bidderId", 1)]]
```

- [ ] **Step 2: Run to verify failure** — `pytest tests_unit/test_bids_schemas.py -v` → FAIL, import error.

- [ ] **Step 3: Implement the two models and register them** (`app/models/__init__.py`, `DOCUMENT_MODELS` in `app/db.py`).

- [ ] **Step 4: Implement `app/bids/schemas.py`.** Use `field_validator`s.
  - Trim strings. `""` becomes `None` for `country` and `profileId`.
  - `country` must be two ASCII letters, upper-cased. `profileId` must pass `PydanticObjectId.is_valid`.
  - Rate check: `0 <= v <= 1000` and `abs(v * 100 - round(v * 100)) < 1e-6`.
  - The folder URL must pass `parse_folder_url`; store the trimmed original.
  - Error messages are the user-facing copy, e.g. "Use a two-letter country code", "Rate must have at most 2 decimals", "Use an s3:// or amazonaws.com folder URL".

- [ ] **Step 5: Run to verify pass** — same command → all PASS.

- [ ] **Step 6: Commit**

```bash
git add app/models/bidder.py app/models/bid_report.py app/models/__init__.py app/db.py app/bids/schemas.py tests_unit/test_bids_schemas.py
git commit -m "feat(bids): Bidder and BidReport models with request validation"
```

---

### Task 4: Slack report formatting

**Files:**
- Create: `app/bids/slack_format.py`
- Test: `tests_unit/test_bids_slack_format.py`

**Interfaces:**
- Consumes: `BidReportRow` (Task 3), `Kind` (Task 1), `_country_flag` from `app/slack_bot.py`, `_fmt_usd` from `app/slack.py`.
- Produces:
  - `@dataclass(frozen=True) class Owner: name: str; label: str` (`label` is a Slack mention or a name; `name` is used for sorting)
  - `UNKNOWN_OWNER = Owner("Unknown owner", "Unknown owner")`
  - `format_report(kind: Kind, start: datetime, end: datetime, rows: Sequence[BidReportRow], owners: Mapping[str, Owner], tz: ZoneInfo, app_url: str) -> tuple[str, list[list[dict]]]`. Returns `(fallback_text, messages)`. `messages[0]` goes to the channel; any further messages are thread replies. `owners` is keyed by `str(ownerId)`.

**Exact copy** (fixed by the spec; `D` = `Fri Oct 2`, `T` = `6:30 PM`, `Z` = `%Z` of the end, e.g. `CDT`):
- Daily header: `📨 *Daily bids — {D(end)}* · {Dow(start)} {T(start)} → {Dow(end)} {T(end)} {Z}`
- Weekly header: `💰 *Weekly bids & pay — week ending {D(end)}* · {D(start)} {T(start)} → {D(end)} {T(end)} {Z}`
- Owner line: daily `*{label}* — {n} bids`; weekly `*{label}* — {n} bids · *{$}*`; append ` · {k} failed` when any row failed.
- Bidder line: `• {flag} {name} · {profile} — {count}`, with the flag and `· profile` omitted when absent. Weekly: `— {count} bids × {$rate} = {$amount}`.
- Error line: `• ⚠️ {name} — couldn't read screenshot folder`
- Footer: daily `*Total: {n} bids · {k} bidders* · <{app_url}/bidders|View in app>` (`k` = all rows, failed included); weekly `*Total: {n} bids · {$}* · <…|View in app>`
- Fallback text: `Daily bids — {D(end)}: {n} bids` / `Weekly bids & pay — week ending {D(end)}: {n} bids · {$}`
- Use `bid` for a count of 1 and `bids` otherwise. Numbers use thousands separators. Money uses `_fmt_usd`.
- Sort owners by `name` (case-insensitive). Within an owner, sort by count descending with failed rows last, then by name. Totals exclude failed rows.

- [ ] **Step 1: Write the failing tests**

```python
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from beanie import PydanticObjectId

from app.bids.slack_format import Owner, format_report
from app.models.bid_report import BidReportRow

CT = ZoneInfo("America/Chicago")
START = datetime(2026, 10, 1, 23, 30, tzinfo=timezone.utc)
END = datetime(2026, 10, 2, 23, 30, tzinfo=timezone.utc)
PEDRO, MARIA = PydanticObjectId(), PydanticObjectId()
OWNERS = {str(PEDRO): Owner("Pedro", "<@U1>"), str(MARIA): Owner("maria", "Maria")}


def row(owner, name, count, rate=0.5, country=None, profile=None, error=None):
    return BidReportRow(kind="daily", periodKey="2026-10-02", periodStart=START, periodEnd=END,
                        bidderId=PydanticObjectId(), ownerId=owner, bidderName=name, country=country,
                        profileName=profile, rate=rate, count=count,
                        amount=None if count is None else round(count * rate, 2), error=error)


def texts(messages):
    return [b["text"]["text"] for m in messages for b in m if b["type"] == "section"]


def test_daily_report_copy():
    rows = [row(PEDRO, "Raj Kumar", 55, country="IN", profile="Backend"),
            row(PEDRO, "Ana Silva", 87, country="BR", profile="AI Full-stack"),
            row(PEDRO, "John Doe", None, error="AccessDenied"),
            row(MARIA, "Mia Cruz", 0)]
    text, messages = format_report("daily", START, END, rows, OWNERS, CT, "https://app.example")
    assert text == "Daily bids — Fri Oct 2: 142 bids"
    t = texts(messages)
    assert t[0] == "📨 *Daily bids — Fri Oct 2* · Thu 6:30 PM → Fri 6:30 PM CDT"
    assert t[1] == "*Maria* — 0 bids\n• Mia Cruz — 0"   # "maria" sorts before "Pedro"
    assert t[2] == ("*<@U1>* — 142 bids · 1 failed\n• 🇧🇷 Ana Silva · AI Full-stack — 87\n"
                    "• 🇮🇳 Raj Kumar · Backend — 55\n• ⚠️ John Doe — couldn't read screenshot folder")
    assert t[-1] == "*Total: 142 bids · 4 bidders* · <https://app.example/bidders|View in app>"


def test_weekly_report_lines_include_pay():
    rows = [row(PEDRO, "Ana Silva", 512, rate=0.5, country="BR", profile="AI Full-stack"),
            row(PEDRO, "Raj Kumar", 1, rate=0.1)]
    text, messages = format_report("weekly", START, END, rows, OWNERS, CT, "https://app.example")
    t = texts(messages)
    assert t[0].startswith("💰 *Weekly bids & pay — week ending Fri Oct 2* · Thu Oct 1 6:30 PM → Fri Oct 2 6:30 PM CDT")
    assert t[1] == ("*<@U1>* — 513 bids · *$256.10*\n• 🇧🇷 Ana Silva · AI Full-stack — 512 bids × $0.50 = $256.00\n"
                    "• Raj Kumar — 1 bid × $0.10 = $0.10")
    assert t[-1] == "*Total: 513 bids · $256.10* · <https://app.example/bidders|View in app>"
    assert text == "Weekly bids & pay — week ending Fri Oct 2: 513 bids · $256.10"


def test_missing_owner_falls_back():
    stranger = PydanticObjectId()
    _, messages = format_report("daily", START, END, [row(stranger, "Lee", 3)], OWNERS, CT, "https://a")
    assert texts(messages)[1] == "*Unknown owner* — 3 bids\n• Lee — 3"


def test_large_reports_respect_slack_limits():
    owners = {str(o): Owner(f"Owner {i}", f"Owner {i}") for i, o in enumerate(PydanticObjectId() for _ in range(60))}
    # 5 bidders × ~620 chars per owner forces the 3,000-char section split; 60 owners force a 2nd message
    rows = [row(PydanticObjectId(o), f"Bidder {i:03d} " + "x" * 600, i)
            for i, o in enumerate(list(owners) * 5)]
    _, messages = format_report("daily", START, END, rows, owners, CT, "https://a")
    assert len(messages) > 1 and all(len(m) <= 50 for m in messages)
    assert all(len(s) <= 3000 for s in texts(messages))
    body = "\n".join(texts(messages))
    assert all(body.count(f"Bidder {i:03d} ") == 1 for i in range(300))
```

- [ ] **Step 2: Run to verify failure** — `pytest tests_unit/test_bids_slack_format.py -v` → FAIL, import error.

- [ ] **Step 3: Implement `format_report`.**
  - Each owner is one `section` block (`mrkdwn`). If an owner's text would exceed 3,000 characters, continue in further sections and repeat the owner line with ` (cont.)`.
  - The header is the first section. The footer is a final `section`.
  - Chunk the block list into messages of at most 50. The footer always ends the last message.
  - Format times without `%-I` (Windows has no such directive): strip the leading `0` from `%I`.

- [ ] **Step 4: Run to verify pass** — same command → all PASS.

- [ ] **Step 5: Commit**

```bash
git add app/bids/slack_format.py tests_unit/test_bids_slack_format.py
git commit -m "feat(bids): daily and weekly Slack report formatting"
```

---

### Task 5: Report tick, cron wiring, and ops doc

**Files:**
- Create: `app/bids/reports.py`, `app/routers/bidders.py` (only `cron_router` in this task), `docs/bids.md`
- Modify: `app/alerts_tick.py` (third step), `app/main.py` (include `bidders.cron_router`), `tests_unit/test_alerts_tick.py`
- Test: `tests_unit/test_bids_reports.py`

**Interfaces:**
- Consumes: Tasks 1–4.
- Produces:
  - `CATCH_UP = timedelta(hours=6)`
  - `report_key(kind: Kind, period_key: str) -> str` → `"bids-daily:2026-10-02"`
  - `class ReportStore(Protocol)`:
    - `async is_done(key: str) -> bool`
    - `async mark_done(key: str) -> None`
    - `async load_rows(kind: Kind, period_key: str) -> list[BidReportRow]`
    - `async save_rows(rows: list[BidReportRow]) -> list[BidReportRow]` (on `DuplicateKeyError`, returns `load_rows(...)`)
    - `async active_bidders() -> list` (bidder-like objects)
    - `async profile_names(ids: list[PydanticObjectId]) -> dict[str, str]`
    - `async owners(ids: list[PydanticObjectId]) -> dict[str, Owner]`
  - `class BeanieReportStore` implements it: `SlackAlertLog` with `CALLER_CHANNEL_SENTINEL`, `BidReport`, `Bidder.find(archivedAt == None)`, `Account`, and `UserExtra` with `Owner(name=user.name or user.email, label=_mention(user))`.
  - `async def build_rows(kind, start, end, period_key, bidders, profile_names, storage) -> list[BidReportRow]` (uses `count_folders`, snapshots name/country/profile/rate, `amount = round(count * rate, 2)`)
  - `bids_slack_configured() -> bool` (`SLACK_BIDS_CHANNEL_ID` and `SLACK_BOT_TOKEN` both set)
  - `async def post_report(text: str, messages: list[list[dict]]) -> None`: `chat.postMessage` to the channel via `_slack_api`, then each further message as a reply with `thread_ts` from the first. Raises on failure.
  - `async def run_bid_reports_tick(now: datetime | None = None, *, store: ReportStore | None = None, storage: BidStorage | None = None, post=None, sched: BidSchedule | None = None) -> dict[str, str]`. Returns `{"daily": status, "weekly": status}`. Each status is one of `not-due`, `done`, `no-bidders`, `posted`, `saved:slack-not-configured`, or `failed:post`.
  - `cron_router = APIRouter(prefix="/internal/bids")`, `POST /tick`, checking the `x-cron-secret` header exactly like `slack_bot_tick` in `app/routers/slack_bot.py`.

- [ ] **Step 1: Write the failing tests** (`tests_unit/test_bids_reports.py`)

```python
from datetime import datetime, time, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from beanie import PydanticObjectId

from app.bids import reports
from app.bids.reports import run_bid_reports_tick
from app.bids.slack_format import Owner
from app.bids.storage import BidStorageError
from app.bids.windows import BidSchedule

CT = ZoneInfo("America/Chicago")
SCHED = BidSchedule(tz=CT, daily_at=time(18, 30), weekly_day=5, weekly_at=time(16, 0))
DAILY_CUTOFF = datetime(2026, 10, 2, 23, 30, tzinfo=timezone.utc)   # Fri 18:30 CDT
OWNER = PydanticObjectId()


def bidder(name, folder, rate=0.5):
    return SimpleNamespace(id=PydanticObjectId(), createdBy=OWNER, name=name, country="BR", profileId=None,
                           rate=rate, screenshotFolderUrl=folder)


class FakeStore:
    def __init__(self, bidders):
        self.bidders, self.rows, self.done = bidders, [], set()

    async def is_done(self, key): return key in self.done
    async def mark_done(self, key): self.done.add(key)
    async def load_rows(self, kind, pk): return [r for r in self.rows if (r.kind, r.periodKey) == (kind, pk)]
    async def save_rows(self, rows): self.rows += rows; return rows
    async def active_bidders(self): return self.bidders
    async def profile_names(self, ids): return {}
    async def owners(self, ids): return {str(OWNER): Owner("Pedro", "Pedro")}


class FakeStorage:
    def __init__(self, counts): self.counts, self.calls = counts, 0

    async def count_screenshots(self, url, start, end):
        self.calls += 1
        c = self.counts[url]
        if isinstance(c, Exception):
            raise c
        return c


class Poster:
    def __init__(self, fail=False): self.fail, self.sent = fail, []

    async def __call__(self, text, messages):
        if self.fail:
            raise RuntimeError("slack down")
        self.sent.append(text)


async def tick(now, store, storage, post):
    return await run_bid_reports_tick(now, store=store, storage=storage, post=post, sched=SCHED)


async def test_not_due_after_catch_up_window():
    store, post = FakeStore([bidder("Ana", "s3://b/ana/")]), Poster()
    got = await tick(DAILY_CUTOFF + timedelta(hours=6, minutes=1), store, FakeStorage({"s3://b/ana/": 3}), post)
    assert got["daily"] == "not-due" and store.rows == [] and post.sent == []


async def test_due_daily_saves_rows_posts_and_marks_done():
    store, storage, post = FakeStore([bidder("Ana", "s3://b/ana/"), bidder("Raj", "s3://b/raj/")]), \
        FakeStorage({"s3://b/ana/": 3, "s3://b/raj/": 0}), Poster()
    got = await tick(DAILY_CUTOFF + timedelta(minutes=5), store, storage, post)
    assert got["daily"] == "posted"
    assert sorted((r.bidderName, r.count) for r in store.rows) == [("Ana", 3), ("Raj", 0)]
    assert "bids-daily:2026-10-02" in store.done and len(post.sent) == 1
    again = await tick(DAILY_CUTOFF + timedelta(minutes=10), store, storage, post)
    assert again["daily"] == "done" and storage.calls == 2 and len(post.sent) == 1


async def test_failed_post_is_retried_with_saved_rows():
    store, storage = FakeStore([bidder("Ana", "s3://b/ana/")]), FakeStorage({"s3://b/ana/": 3})
    first = await tick(DAILY_CUTOFF + timedelta(minutes=5), store, storage, Poster(fail=True))
    assert first["daily"] == "failed:post" and store.done == set() and len(store.rows) == 1
    post = Poster()
    second = await tick(DAILY_CUTOFF + timedelta(minutes=10), store, storage, post)
    assert second["daily"] == "posted" and storage.calls == 1 and len(store.rows) == 1


async def test_one_unreadable_folder_does_not_stop_the_report():
    store = FakeStore([bidder("Ana", "s3://b/ana/"), bidder("John", "s3://b/john/")])
    storage = FakeStorage({"s3://b/ana/": 3, "s3://b/john/": BidStorageError("AccessDenied")})
    got = await tick(DAILY_CUTOFF + timedelta(minutes=5), store, storage, Poster())
    john = next(r for r in store.rows if r.bidderName == "John")
    assert got["daily"] == "posted" and john.count is None and john.error == "AccessDenied" and john.amount is None


async def test_no_bidders_marks_done_without_posting():
    store, post = FakeStore([]), Poster()
    got = await tick(DAILY_CUTOFF + timedelta(minutes=5), store, FakeStorage({}), post)
    assert got["daily"] == "no-bidders" and "bids-daily:2026-10-02" in store.done and post.sent == []


async def test_slack_not_configured_saves_but_does_not_mark(monkeypatch):
    monkeypatch.setattr(reports, "bids_slack_configured", lambda: False)
    store = FakeStore([bidder("Ana", "s3://b/ana/")])
    got = await tick(DAILY_CUTOFF + timedelta(minutes=5), store, FakeStorage({"s3://b/ana/": 3}), None)
    assert got["daily"] == "saved:slack-not-configured" and len(store.rows) == 1 and store.done == set()


async def test_amount_is_rounded_rate_snapshot():
    store = FakeStore([bidder("Ana", "s3://b/ana/", rate=0.1)])
    await tick(DAILY_CUTOFF + timedelta(minutes=5), store, FakeStorage({"s3://b/ana/": 3}), Poster())
    assert store.rows[0].rate == 0.1 and store.rows[0].amount == 0.3


async def test_saturday_evening_runs_daily_and_weekly():
    # Sat 2026-10-03 18:35 CDT: daily cutoff 5 min ago, weekly cutoff (16:00) 2h35m ago
    now = datetime(2026, 10, 3, 23, 35, tzinfo=timezone.utc)
    store, post = FakeStore([bidder("Ana", "s3://b/ana/")]), Poster()
    got = await tick(now, store, FakeStorage({"s3://b/ana/": 3}), post)
    assert got == {"daily": "posted", "weekly": "posted"}
    assert {"bids-daily:2026-10-03", "bids-weekly:2026-10-03"} <= store.done


def test_cron_endpoint_rejects_wrong_secret(monkeypatch):
    from fastapi import HTTPException
    from app.routers import bidders
    monkeypatch.setattr(bidders, "get_settings", lambda: SimpleNamespace(SLACK_BOT_CRON_SECRET="s3cret"))
    with pytest.raises(HTTPException) as exc:
        import asyncio
        asyncio.run(bidders.bids_tick(x_cron_secret="wrong"))
    assert exc.value.status_code == 401
```

Update `tests_unit/test_alerts_tick.py`: both existing tests also patch `app.bids.reports.run_bid_reports_tick`. The order test asserts `["slack", "alerts", "bids"]`. Add `test_run_cron_ticks_reports_bid_failure_after_other_steps`: bids raises, so the result is `1`, and slack and alerts were both awaited.

- [ ] **Step 2: Run to verify failure** — `pytest tests_unit/test_bids_reports.py tests_unit/test_alerts_tick.py -v` → FAIL (import errors; order test fails).

- [ ] **Step 3: Implement `app/bids/reports.py`** following spec §5 steps 1–6, once per kind:
  - Read the settings once per tick.
  - `post` resolves to `post_report` when `post is None and bids_slack_configured()`; otherwise it stays `None`.
  - `owners` and `profile_names` are only fetched when rows are being built or posted.
  - Log one `INFO` line per kind with its status.

- [ ] **Step 4: Wire it up.**
  - Add the third `try/except` step to `run_cron_ticks()`: `from app.bids.reports import run_bid_reports_tick`, log `"bid reports tick %s"`, and set `code = 1` on exception.
  - Create `app/routers/bidders.py` with `cron_router` and `bids_tick(x_cron_secret)`, and include it in `app/main.py` next to `slack_bot.cron_router`.

- [ ] **Step 5: Write `docs/bids.md`** in the style of `docs/slack-interview-bot.md`. Cover:
  - the env keys
  - that the bot must be invited to the bids channel
  - the read-only IAM permission (`s3:ListBucket` on the bucket, with the bidders' prefixes)
  - the catch-up behaviour
  - the cron fallback `curl -X POST -H "x-cron-secret: …" $APP_BASE_URL/internal/bids/tick`

- [ ] **Step 6: Run to verify pass** — `pytest tests_unit -q` → all PASS (whole suite, since `alerts_tick` changed).

- [ ] **Step 7: Commit**

```bash
git add app/bids/reports.py app/routers/bidders.py app/main.py app/alerts_tick.py docs/bids.md tests_unit/test_bids_reports.py tests_unit/test_alerts_tick.py
git commit -m "feat(bids): daily/weekly report tick on the cron with Slack posting"
```

---

### Task 6: Bidders API and live counts

**Files:**
- Create: `app/bids/live_counts.py`
- Modify: `app/routers/bidders.py` (add `router`), `app/main.py` (include `bidders.router`)
- Test: `tests_unit/test_bids_live_counts.py`, `tests_unit/test_bids_router.py`

**Interfaces:**
- Consumes: Tasks 1–3 (`open_window`, `count_folders`, `get_bid_storage`, `BidderIn`, `bidder_out`, `report_out`, models).
- Produces:
  - `class TtlCache: __init__(self, ttl: timedelta)`, `get(key, now) -> Any | None`, `set(key, value, now) -> None`
  - `async def live_counts(bidders: Sequence, now: datetime, sched: BidSchedule, storage: BidStorage, cache: TtlCache) -> dict[str, dict]`. Each value is `{"today": int|None, "week": int|None, "error": str|None}`. The cache key is `(str(id), screenshotFolderUrl, kind, window_start.isoformat())`, and errors are cached too.
  - `ensure_owner(bidder, user) -> bidder`: raises `HTTPException(404, "bidder not found")` when the bidder is `None` or not owned by the user. Archived bidders pass (history stays readable). Admins get no exemption.
  - `can_select_profile(account, user) -> bool`: not `None`, not `archived`, and (`user.role == "admin"` or `account.createdBy == user.id`).
  - Routes (`router = APIRouter(prefix="/bidders", tags=["bidders"])`). Declare `/live-counts` before `/{bidder_id}`.
    - `GET ""` → `{"bidders": [bidder_out…]}`
    - `POST ""` → bidder_out
    - `PUT "/{bidder_id}"` → bidder_out
    - `DELETE "/{bidder_id}"` → 204
    - `GET "/live-counts"` → map from `live_counts`
    - `GET "/{bidder_id}/reports"` → `{"reports": [report_out…]}`
  - Invalid profile → `HTTPException(422, detail=[{"loc": ["body", "profileId"], "msg": "Choose one of your profiles", "type": "value_error"}])`, the same shape as FastAPI's own 422s.

- [ ] **Step 1: Write the failing tests**

`tests_unit/test_bids_live_counts.py`:
```python
# SCHED and utc() as in test_bids_windows.py
from app.bids.live_counts import TtlCache, live_counts
from app.bids.storage import BidStorageError

NOW = utc(2026, 10, 3, 3, 0)  # Fri Oct 2 22:00 CDT


class Storage:
    def __init__(self, result=4): self.result, self.calls = result, []

    async def count_screenshots(self, url, start, end):
        self.calls.append((url, start, end))
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def bidder(url="s3://b/ana/"):
    return SimpleNamespace(id=PydanticObjectId(), screenshotFolderUrl=url)


def cache():
    return TtlCache(timedelta(minutes=5))


async def test_live_counts_use_open_windows():
    b, storage = bidder(), Storage()
    got = await live_counts([b], NOW, SCHED, storage, cache())
    assert got == {str(b.id): {"today": 4, "week": 4, "error": None}}
    assert sorted((s, e) for _, s, e in storage.calls) == [(utc(2026, 9, 26, 21, 0), NOW), (utc(2026, 10, 2, 23, 30), NOW)]


async def test_live_counts_are_cached_for_five_minutes():
    b, storage, c = bidder(), Storage(), cache()
    await live_counts([b], NOW, SCHED, storage, c)
    await live_counts([b], NOW + timedelta(minutes=4), SCHED, storage, c)
    assert len(storage.calls) == 2
    await live_counts([b], NOW + timedelta(minutes=6), SCHED, storage, c)
    assert len(storage.calls) == 4


async def test_changing_the_folder_url_bypasses_the_cache():
    b, storage, c = bidder(), Storage(), cache()
    await live_counts([b], NOW, SCHED, storage, c)
    b.screenshotFolderUrl = "s3://b/ana-new/"
    await live_counts([b], NOW + timedelta(minutes=1), SCHED, storage, c)
    assert len(storage.calls) == 4 and storage.calls[-1][0] == "s3://b/ana-new/"


async def test_live_count_error_is_reported():
    b = bidder()
    got = await live_counts([b], NOW, SCHED, Storage(BidStorageError("NoSuchBucket")), cache())
    assert got == {str(b.id): {"today": None, "week": None, "error": "NoSuchBucket"}}
```

`tests_unit/test_bids_router.py`:
```python
from types import SimpleNamespace
import pytest
from beanie import PydanticObjectId
from fastapi import HTTPException
from app.routers.bidders import can_select_profile, ensure_owner

ME, OTHER = PydanticObjectId(), PydanticObjectId()
user = SimpleNamespace(id=ME, role="staff")
admin = SimpleNamespace(id=OTHER, role="admin")


def test_ensure_owner():
    mine = SimpleNamespace(createdBy=ME)
    assert ensure_owner(mine, user) is mine
    for b, u in [(None, user), (SimpleNamespace(createdBy=OTHER), user), (mine, admin)]:
        with pytest.raises(HTTPException) as exc:
            ensure_owner(b, u)
        assert exc.value.status_code == 404


def test_can_select_profile():
    own = SimpleNamespace(createdBy=ME, archived=False)
    assert can_select_profile(own, user)
    assert not can_select_profile(None, user)
    assert not can_select_profile(SimpleNamespace(createdBy=ME, archived=True), user)
    assert not can_select_profile(SimpleNamespace(createdBy=OTHER, archived=False), user)
    assert can_select_profile(SimpleNamespace(createdBy=ME, archived=False), admin)
```

- [ ] **Step 2: Run to verify failure** — `pytest tests_unit/test_bids_live_counts.py tests_unit/test_bids_router.py -v` → FAIL, import errors.

- [ ] **Step 3: Implement `app/bids/live_counts.py`.** Use a module-level `CACHE = TtlCache(timedelta(minutes=5))` for the router. For each kind, call `count_folders` only for the cache misses. Combine errors: `error` is the first non-`None` error among the bidder's two counts.

- [ ] **Step 4: Implement the routes** in `app/routers/bidders.py`, with `user: Annotated[UserExtra, Depends(get_current_user)]` on each.
  - **List:** `Bidder.find(createdBy == user.id)`, filtered on `archivedAt == None` or `!= None` depending on `?archived=`. Sort by name. Join profile names with one `Account.find({"_id": {"$in": ids}})`.
  - **Create/update:** body is `BidderIn`. If `profileId` is set, it must pass `can_select_profile(await Account.get(...), user)`. Update loads the bidder, runs `ensure_owner`, sets the fields, then `save()`.
  - **Archive:** sets `archivedAt` once, so repeat calls are no-ops.
  - **Reports:** `ensure_owner`, `kind: Literal["daily","weekly"] = "daily"`, `limit: int = Query(30, ge=1, le=200)`. Sort `periodKey` descending.
  - **Live counts:** the user's active bidders, then `live_counts(..., datetime.now(timezone.utc), schedule_from_settings(), get_bid_storage(), CACHE)`.

  Include `bidders.router` in `app/main.py`.

- [ ] **Step 5: Run to verify pass** — `pytest tests_unit -q` → all PASS. Then smoke-test the app boots: `.venv/Scripts/python.exe -c "from app.main import app; print(sorted(r.path for r in app.routes if 'bid' in r.path))"` → prints the 6 `/bidders…` paths and `/internal/bids/tick`.

- [ ] **Step 6: Commit**

```bash
git add app/bids/live_counts.py app/routers/bidders.py app/main.py tests_unit/test_bids_live_counts.py tests_unit/test_bids_router.py
git commit -m "feat(bids): bidders CRUD, history and cached live counts API"
```

---

### Task 7: Bidders page with add/edit/archive (`engineers_app`)

**Files:**
- Modify: `src/api/endpoints.ts` (new `// ---------- bidders ----------` section), `src/App.tsx` (route), `src/components/Sidebar.tsx` (`NavLink` after "Job Applies")
- Create: `src/pages/Bidders.tsx`, `src/components/bidders/BidderFormModal.tsx`

**Interfaces:**
- Consumes: the Task 6 API.
- Produces (in `endpoints.ts`):
```ts
export interface Bidder { _id: string; name: string; country: string | null; profileId: string | null;
  profileName: string | null; rate: number; screenshotFolderUrl: string; archivedAt: string | null; createdAt: string }
export type BidderInput = Pick<Bidder, 'name' | 'country' | 'profileId' | 'rate' | 'screenshotFolderUrl'>;
export interface BidderLiveCount { today: number | null; week: number | null; error: string | null }
export interface BidReport { kind: 'daily' | 'weekly'; periodKey: string; periodStart: string; periodEnd: string;
  count: number | null; rate: number; amount: number | null; error: string | null }
export const listBidders: (archived?: boolean) => Promise<{ bidders: Bidder[] }>;
export const createBidder: (body: BidderInput) => Promise<Bidder>;
export const updateBidder: (id: string, body: BidderInput) => Promise<Bidder>;
export const archiveBidder: (id: string) => Promise<null>;
export const bidderLiveCounts: () => Promise<Record<string, BidderLiveCount>>;
export const bidderReports: (id: string, kind: 'daily' | 'weekly', limit?: number) => Promise<{ reports: BidReport[] }>;
```
- `BidderFormModal({ open, bidder, onClose, onSaved }: { open: boolean; bidder: Bidder | null; onClose: () => void; onSaved: () => void })`. A `null` bidder means create mode.

- [ ] **Step 1: Add the endpoint types and functions** using the existing `apiFetch`, `postJSON`, `putJSON`, and `del` helpers.

- [ ] **Step 2: Build `BidderFormModal`.**
  - Fields: name (text), country (`CountrySelect`), profile (`Select` with options from `lookupAccounts()`, filtered to `!archived && (createdBy === me.id || me.role === 'admin')`, plus a "No profile" option), rate (number, `step="0.01"`, `min=0`, `$` prefix), screenshot folder URL (placeholder `s3://bucket/bids/ana-silva/`).
  - On submit, call create or update. On `ApiError` with status 422 whose `body.detail` is an array, map each item's last `loc` entry to that field's error text under the input. Otherwise toast the error's message.
  - On success: toast "Bidder saved", then `onSaved()`.

- [ ] **Step 3: Build `src/pages/Bidders.tsx`.**
  - Header: `PageHeader` titled "Bidders", with an **Add bidder** button as `action` and a `Switch` labelled "Show archived".
  - Data: SWR `['bidders', archived]` → `listBidders(archived)`, and SWR `'bidder-live-counts'` → `bidderLiveCounts()` with `refreshInterval: 300_000`.
  - Table columns:
    - Name
    - Country (flag + code)
    - Profile
    - Rate (`$0.50`)
    - Today and This week: `LoadingSpinner` until counts load, ⚠️ with `title={error}` on error
    - Est. pay this week: `week × rate`, formatted `$1,234.50`, `—` when unknown
    - `ActionMenu`: Edit, History, Archive. Archived rows are greyed and show History only.
  - Archive goes through `ConfirmDialog` with body "Archive {name}? They'll stop appearing in reports. History is kept." and `tone="danger"`, then revalidates.
  - Empty state: "No bidders yet — add one to start tracking bids."
  - History opens the panel from Task 8. Until that exists, keep `historyFor` state and leave the panel out.

- [ ] **Step 4: Add the `/bidders` route** (`<Protected><Bidders /></Protected>`) **and a Sidebar `NavLink`** (label "Bidders", a people-style SVG matching the other 24×24 stroke icons).

- [ ] **Step 5: Verify** — `npm run build` in `engineers_app` → exits 0 with no TypeScript errors.

- [ ] **Step 6: Commit**

```bash
git add src/api/endpoints.ts src/App.tsx src/components/Sidebar.tsx src/pages/Bidders.tsx src/components/bidders/BidderFormModal.tsx
git commit -m "feat(bidders): bidders page with add, edit, archive and live counts"
```

---

### Task 8: History panel and end-to-end check

**Files:**
- Create: `engineers_app/src/components/bidders/BidderHistoryPanel.tsx`
- Modify: `engineers_app/src/pages/Bidders.tsx` (render the panel)

**Interfaces:**
- Consumes: `bidderReports`, `Bidder`, `BidReport` (Task 7).
- Produces: `BidderHistoryPanel({ bidder, onClose }: { bidder: Bidder | null; onClose: () => void })`. It is open whenever `bidder` is non-null.

- [ ] **Step 1: Build the panel.**
  - `SidePanel` titled with the bidder's name. The subtitle shows the folder URL in monospace with a copy button (`navigator.clipboard.writeText`, toast "Folder URL copied").
  - `Tabs`: **Daily** and **Weekly**. Each tab loads `bidderReports(id, kind, 30)` through SWR `['bidder-reports', id, kind]`.
  - Daily rows: date (`periodKey`), window (`Thu 6:30 PM → Fri 6:30 PM`, local formatting of `periodStart`/`periodEnd`), count.
  - Weekly rows: week ending, count, rate, pay.
  - Failed rows show `⚠️ {error}` instead of a count. An empty tab shows "No reports yet — the first one posts at the next cutoff."

- [ ] **Step 2: Render the panel from `Bidders.tsx`** using its `historyFor` state.

- [ ] **Step 3: Verify the build** — `npm run build` → exits 0.

- [ ] **Step 4: End-to-end check against a running backend.** Use a local `.env` with Mongo and S3 credentials, and set `SLACK_BIDS_CHANNEL_ID` to a test channel. Run `uvicorn app.main:app --port 8080` and `npm run dev`, then confirm each item:
  - Create a bidder with an invalid folder URL. The URL field shows the 422 message.
  - Create a bidder with a real folder. Today and This week fill in.
  - Create one with a valid-looking but wrong path. ⚠️ appears, with the storage error as tooltip.
  - Edit the rate. Est. pay updates.
  - Archive the bidder. It disappears. "Show archived" shows it greyed, with History only.
  - `curl -X POST -H "x-cron-secret: $SLACK_BOT_CRON_SECRET" localhost:8080/internal/bids/tick` within 6 h after a cutoff (or with `BID_DAILY_REPORT_TIME` set to a few minutes ago). The report appears in the test channel. A second curl returns `"daily": "done"`. The History panel shows the saved row.

- [ ] **Step 5: Commit**

```bash
git add src/components/bidders/BidderHistoryPanel.tsx src/pages/Bidders.tsx
git commit -m "feat(bidders): per-bidder daily and weekly history panel"
```
