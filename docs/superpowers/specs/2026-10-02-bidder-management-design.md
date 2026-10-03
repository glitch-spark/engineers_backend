# Bidders — bidder management and bid reports

Date: 2026-10-02
Branches: `feat/bid-manage` in `engineers_backend` (from `dev`) and `engineers_app` (from `main`)
Status: approved design, pending spec review

## 1. Goal

Users manage the bidders who apply to jobs for them. A separate system (outside these repos) saves a
screenshot each time a bidder submits an application, into one storage folder per bidder. This feature
counts those screenshots and posts:

- a **daily** bid count per bidder to a Slack channel, and
- a **weekly** bid count per bidder with the amount to pay them (`bids × rate`).

A new page lets each user create, edit and archive their bidders, see live counts for the current day and
week, and browse past daily and weekly results.

### Decisions already made

| Topic | Decision |
|---|---|
| Screenshot source | The existing external system writes screenshots to S3, one folder per bidder. We only count them. Each bidder stores its folder URL. |
| Storage provider | S3 today, likely replaced by a cheaper provider. Reads go through a one-method adapter. |
| What counts as a bid | One image file (`.png .jpg .jpeg .webp`, case-insensitive) whose upload time falls inside the window. Screenshots are not inspected. |
| Rate | USD per bid. Weekly pay = weekly count × rate. |
| Daily window | 6:30 PM → 6:30 PM, `America/Chicago`. Reported at 6:30 PM. |
| Weekly window | Saturday 4:00 PM → Saturday 4:00 PM, `America/Chicago`. Reported Saturday 4:00 PM. Counted directly from storage, independent of the daily windows. |
| Configurable | Timezone, daily time, weekly day and time, Slack channel — all in `.env`. |
| Ownership | Every logged-in user manages their own bidders. Bidders do not log in. |
| Slack | One shared channel. Each report has one section per owner. |
| Page | Bidder CRUD + live counts + per-bidder history (approach B). No payment tracking. |
| Counting approach | Count at report time and save snapshots (approach 1). No per-screenshot index. |
| Delete | Archives the bidder; history stays. |

### Out of scope

A `POST /bids` upload API (possible follow-up; it would become a second count source), screenshot review
or validation, marking weeks as paid, restoring archived bidders, admin views across users, editing past
reports, per-bidder currencies, backfilling reports older than the catch-up window.

## 2. Data model

### `Bidder` — `app/models/bidder.py`, collection `bidders`

Extends `TimestampedDocument`.

| Field | Type | Notes |
|---|---|---|
| `createdBy` | `PydanticObjectId` | Owner. Indexed. Every query filters on it. |
| `name` | `str` | Required, trimmed, non-empty. |
| `country` | `Optional[str]` | ISO 3166-1 alpha-2, upper-cased (same format as `Account.country`). |
| `profileId` | `Optional[PydanticObjectId]` | The `Account` (profile) the bidder applies with. |
| `rate` | `float` | USD per bid, `>= 0`, at most 2 decimals. |
| `screenshotFolderUrl` | `str` | Must parse (section 3). |
| `archivedAt` | `Optional[datetime]` | Set by delete. Archived bidders are excluded from the list (unless asked) and from reports. |

Indexes: `[("createdBy", 1), ("archivedAt", 1)]`.

### `BidReport` — `app/models/bid_report.py`, collection `bidreports`

One row per bidder per reported period.

| Field | Type | Notes |
|---|---|---|
| `kind` | `"daily" \| "weekly"` | |
| `periodKey` | `str` | Local date (`YYYY-MM-DD`, report timezone) of the window's end. |
| `periodStart`, `periodEnd` | `datetime` | Exact window in UTC, `[start, end)`. |
| `bidderId` | `PydanticObjectId` | |
| `ownerId` | `PydanticObjectId` | The bidder's `createdBy` at report time. |
| `bidderName` | `str` | Snapshot. |
| `country` | `Optional[str]` | Snapshot (for the flag in Slack and history). |
| `profileName` | `Optional[str]` | Snapshot. |
| `rate` | `float` | Snapshot of the rate at report time. A rate change mid-week applies to the whole week. |
| `count` | `Optional[int]` | `None` when the folder could not be read. |
| `amount` | `Optional[float]` | `round(count × rate, 2)`; `None` when `count` is `None`. Stored for both kinds; the weekly value is the payout. |
| `error` | `Optional[str]` | Short reason when `count` is `None`. |

Indexes: unique `[("bidderId", 1), ("kind", 1), ("periodKey", 1)]` (also serves newest-first history by a
backward scan); `[("kind", 1), ("periodKey", 1)]` for loading a period. Beanie merges indexes with the same
field set, so no second index may use exactly these three fields.

Both models are registered in `app/db.py` alongside the existing documents.

### Slack de-duplication

Reuses `SlackAlertLog` (unique on `userId` + `kind`), with the channel sentinel `userId`
(`CALLER_CHANNEL_SENTINEL` in `app/slack_bot.py`) already used for caller reminders and kinds `bids-daily:<periodKey>` / `bids-weekly:<periodKey>`.

## 3. Storage adapter — `app/bids/storage.py`

```python
class BidStorageError(Exception): ...

class BidStorage(Protocol):
    async def count_screenshots(self, folder_url: str, start: datetime, end: datetime) -> int: ...

def parse_folder_url(url: str) -> tuple[str, str]: ...   # (bucket, prefix) or ValueError
def get_bid_storage() -> BidStorage: ...
```

- **Accepted URL forms** (`parse_folder_url`):
  - `s3://<bucket>/<prefix>`
  - `https://<bucket>.s3.<region>.amazonaws.com/<prefix>` and `https://<bucket>.s3.amazonaws.com/<prefix>`
  - `https://s3.<region>.amazonaws.com/<bucket>/<prefix>` (path style)

  The prefix is URL-decoded and always ends with `/`, so `alice` can never match `alice2/`. An empty
  prefix (whole bucket) is rejected. Anything else raises `ValueError` with a message the API returns as a
  422.
- **`S3BidStorage`** pages through `list_objects_v2(Bucket, Prefix)` and counts keys with an image
  extension whose `LastModified` is in `[start, end)`. Subfolders under the prefix are included. boto3 runs
  in `asyncio.to_thread`. Any boto/network error is re-raised as `BidStorageError` with a short message
  (e.g. `AccessDenied`, `NoSuchBucket`).
- **`get_bid_storage()`** returns the implementation named by `BID_STORAGE_PROVIDER` (only `s3` today).
  Unknown provider → `BidStorageError` on use, so the app still boots.
- **Credentials**: `BID_STORAGE_ACCESS_KEY_ID`, `BID_STORAGE_SECRET_ACCESS_KEY`, `BID_STORAGE_REGION`, each
  falling back to `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_REGION`. Only read access is needed.

## 4. Settings — `app/config.py`

```
BID_STORAGE_PROVIDER=s3
BID_STORAGE_ACCESS_KEY_ID=            # optional, falls back to AWS_ACCESS_KEY_ID
BID_STORAGE_SECRET_ACCESS_KEY=        # optional, falls back to AWS_SECRET_ACCESS_KEY
BID_STORAGE_REGION=                   # optional, falls back to AWS_REGION
BID_REPORT_TIMEZONE=America/Chicago
BID_DAILY_REPORT_TIME=18:30           # HH:MM, 24h, in BID_REPORT_TIMEZONE
BID_WEEKLY_REPORT_DAY=saturday        # monday … sunday
BID_WEEKLY_REPORT_TIME=16:00
SLACK_BIDS_CHANNEL_ID=                # unset = reports are saved but not posted
```

Invalid values (bad `HH:MM`, unknown day or timezone) fail settings validation at startup with a clear
message.

## 5. Windows and the report tick — `app/bids/reports.py`

### Windows

- `latest_daily_cutoff(now)`: today's `BID_DAILY_REPORT_TIME` in the report timezone if `now` is at or
  after it, otherwise yesterday's. Window = `[cutoff − 1 local day, cutoff)`.
- `latest_weekly_cutoff(now)`: the most recent `BID_WEEKLY_REPORT_DAY` at `BID_WEEKLY_REPORT_TIME` at or
  before `now`. Window = `[cutoff − 7 local days, cutoff)`.
- Window ends are built as local wall-clock times and then converted to UTC. A day spanning a DST change
  is therefore 23 or 25 hours, and the cutoff always lands on the configured wall-clock time.
- `periodKey` = the cutoff's local date.

### `run_bid_reports_tick(now=None)`

Called each cron run. For each kind (daily, weekly):

1. Compute the latest cutoff. Skip if `now − cutoff >= 6h` (catch-up window: covers missed cron runs
   without posting stale reports on first deploy).
2. Skip if `SlackAlertLog` already has `bids-<kind>:<periodKey>`.
3. If `BidReport` rows already exist for `(kind, periodKey)`, reuse them. Otherwise, for every
   non-archived bidder, count its window via the adapter and insert a row (a `BidStorageError` becomes
   `count=None, error=<message>`; other bidders continue). Insert uses the unique index, so a concurrent
   run cannot double-insert; a duplicate-key error means another run got there first and the existing rows
   are read instead.
4. If there are no rows (no active bidders), write the `SlackAlertLog` entry without posting and stop, so
   later ticks in the catch-up window don't re-check the period.
5. If `SLACK_BIDS_CHANNEL_ID` or `SLACK_BOT_TOKEN` is unset, stop after saving rows (no post, no log entry).
6. Post the report with `chat.postMessage`. Only on success, write the `SlackAlertLog` entry. A failed post
   is logged and retried on the next tick with the same saved rows.

Returns a small summary dict (kinds run, rows created, posted) for the cron log.

### Wiring

- `app/alerts_tick.py`: `run_cron_ticks()` calls `run_bid_reports_tick()` as a third step in its own
  `try/except`, so a failure cannot block the Slack digest or in-app alerts.
- HTTP fallback: `POST /internal/bids/tick`, protected by `SLACK_BOT_CRON_SECRET` the same way as
  `/internal/slack-bot/tick`.

## 6. Slack messages — `app/bids/slack_format.py`

Block Kit, plus a plain-text `text` fallback. Owners are sorted by name, bidders by count (descending), then
name. Owners are shown with `_mention` (Slack @-mention when linked, name otherwise). Country uses the
existing flag helper.

**Daily**

```
📨 Daily bids — Fri Oct 2 · Thu 6:30 PM → Fri 6:30 PM CT
@pedro — 142 bids
 • 🇧🇷 Ana Silva · AI Full-stack — 87
 • 🇮🇳 Raj Kumar · Backend — 55
 • 🇵🇭 Mia Cruz · Backend — 0
 • ⚠️ John Doe — couldn't read screenshot folder
@maria — 64 bids
 • …
Total: 206 bids · 5 bidders · <View in app>
```

**Weekly**

```
💰 Weekly bids & pay — week ending Sat Oct 3 · Sat Sep 26 4:00 PM → Sat Oct 3 4:00 PM CT
@pedro — 870 bids · $412.50
 • 🇧🇷 Ana Silva · AI Full-stack — 512 bids × $0.50 = $256.00
 • …
Total: 1,240 bids · $598.00 · <View in app>
```

- Bidders with 0 bids are listed. Failed bidders show ⚠️ and are excluded from totals; the owner line
  adds `· 1 failed` when any failed.
- "View in app" links to `${FRONTEND_ORIGIN}/bidders`.
- A long report is split across several section blocks so no block exceeds Slack's 3,000-character text
  limit; above 50 blocks the remainder goes in a thread reply.

## 7. API — `app/routers/bidders.py`

All routes require login (`get_current_user`). Bidder access uses `createdBy == current user`; anything
else is **404**. Profile choices follow the job-applies rule: the user's own accounts (admins: any).

| Method | Path | Behaviour |
|---|---|---|
| `GET` | `/bidders?archived=false` | The user's bidders (active by default), each with `profileName` joined in. Sorted by name. |
| `POST` | `/bidders` | Create. Returns the bidder. |
| `PUT` | `/bidders/{id}` | Full update of the editable fields. |
| `DELETE` | `/bidders/{id}` | Archive (`archivedAt = now`). 204. Idempotent. |
| `GET` | `/bidders/live-counts` | `{ "<bidderId>": { "today": int\|null, "week": int\|null, "error": str\|null } }` for the user's active bidders, using the **current open** windows (`[latest cutoff, now)`). Results cached in-process for 5 minutes per bidder. Bidders are counted concurrently (limit 5). |
| `GET` | `/bidders/{id}/reports?kind=daily&limit=30` | Saved `BidReport` rows, newest first. `limit` ≤ 200. Works for archived bidders. |
| `POST` | `/internal/bids/tick` | Cron fallback (section 5). |

**Validation** (422 with field-level detail):
- `name` must be non-empty after trimming, max 100 characters.
- `country` must be two letters (stored upper-case).
- `rate` must be ≥ 0, at most 2 decimals, at most 1000.
- `profileId` must be a profile the user can select.
- `screenshotFolderUrl` must pass `parse_folder_url`.

The storage itself is not contacted on save. A wrong path shows up immediately as an error in that
bidder's live counts.

## 8. Page — `engineers_app`

- Route `/bidders` (protected) in `src/App.tsx`. A **Bidders** entry in `Sidebar`.
- Endpoints are added to `src/api/endpoints.ts`. Data is loaded with SWR; live counts use
  `refreshInterval: 300000`.
- New files: `src/pages/Bidders.tsx`, `src/components/bidders/BidderFormModal.tsx`,
  `src/components/bidders/BidderHistoryPanel.tsx`, types in `src/types`.

**Layout**

- `PageHeader`: title "Bidders", **Add bidder** button, "Show archived" `Switch`.
- Table: Name · Country (flag + code) · Profile · Rate ($/bid) · **Today** · **This week** ·
  **Est. pay this week** (this week × rate) · `ActionMenu` (Edit, History, Archive).
  Live columns show a spinner until loaded and ⚠️ with the error as a tooltip on failure. Archived rows
  (when shown) are greyed and only offer History.
- **Add/Edit** — `Modal` with name, `CountrySelect`, profile `Select`, rate (number, step 0.01, `$` prefix),
  screenshot folder URL (placeholder `s3://bucket/bids/ana-silva/`). Server 422 messages appear under their
  fields. Success toasts and revalidates.
- **Archive** — `ConfirmDialog` ("Archive Ana Silva? They'll stop appearing in reports. History is kept.").
- **History** — `SidePanel` with `Tabs`: *Daily* (date, window, count) and *Weekly* (week ending, count,
  rate, pay). Failed rows show the error. Header shows the folder URL with a copy button (S3 URLs are
  not browser-openable).
- Empty state: "No bidders yet — add one to start tracking bids."

## 9. Error handling summary

| Situation | Behaviour |
|---|---|
| One bidder's folder unreadable | That row stores `error`; report still posts; ⚠️ in Slack and the page. |
| Storage provider misconfigured | Every row errors; report posts with all ⚠️ so someone notices. |
| Slack post fails | Rows kept, no log entry, retried next tick (within the 6 h window). |
| Cron misses the cutoff | Next tick within 6 h runs it. After 6 h the period is skipped. |
| Two ticks overlap | Unique index on report rows plus the log entry prevent double counting and double posting. |
| Bidder archived mid-week | Not in that week's report (only active bidders at report time are counted). |

## 10. Testing

Backend, `tests_unit/` (pytest, no network, fake clock):

- **Windows**: daily and weekly cutoffs and windows, including both DST transitions in `America/Chicago`;
  catch-up boundaries (tick at cutoff − 5 min, at cutoff, at +5 h 59 min, at +6 h 1 min).
- **Storage**: `parse_folder_url` for every accepted form and rejects (empty prefix, other hosts, garbage);
  `S3BidStorage` with a stubbed paginator — window filtering, extension filtering, pagination, sibling
  prefix not matched, boto error → `BidStorageError`.
- **Report run** (in-memory fakes for storage and Slack): creates rows once and reuses them on retry; a
  failing bidder doesn't stop others; Slack failure leaves no log entry; no bidders → nothing posted;
  archived bidders excluded; rate snapshot used for `amount`.
- **Slack formatting**: daily and weekly output for multiple owners, zero counts, errors, totals,
  block splitting.
- **Router**: CRUD happy paths, each validation rule, another user's bidder → 404, archive idempotent,
  live-counts shape and caching, reports pagination.

Frontend: `npm run build` (type-check), then a manual pass in the running app: create, edit, archive,
show archived, live counts with a valid and an invalid folder, history panel.
