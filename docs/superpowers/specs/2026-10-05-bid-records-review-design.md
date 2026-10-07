# Bid records and daily review: count approved bids, not Backblaze folders

Date: 2026-10-05
Branches: `feat/bidder-login` in `engineers_backend` and `engineers_app`; the Bid Track extension
(`Engineer/Bid Track (BackBlaze)`)
Builds on: `2026-10-03-bidder-extension-login-design.md` (invite codes, `/ext` API, backend-signed uploads)
Status: design approved in conversation, pending spec review

## 1. Goal

Every application a bidder records with the Bid Track extension becomes a **bid record** in the Engineers
backend: who, when, which job (URL and title) and its screenshots. Owners review each day's bids, look at the
screenshots and approve or reject each one. **A day's bid count, and the pay that follows from it, is the number
of approved bids**, replacing today's count of job folders in Backblaze.

### Decisions already made

| Topic | Decision |
|---|---|
| Unit | **One bid per application**: created by its first screenshot, holding every step screenshot of that job. The Submit screenshot marks it submitted. |
| No Submit screenshot | Shown in review, flagged "No submit screenshot"; can still be approved. |
| Reviewers | The bidder's owner (`Bidder.createdBy`) and admins. |
| Pending bids | Count 0. Reports show approved, pending and rejected; only approved counts toward bids and pay. |
| History | Start fresh on rollout day. Earlier days keep their folder-based counts; no records are imported. |
| Release | Together with `feat/bidder-login` (one switch to the new extension for bidders). |
| Approach | The signed-upload request creates/updates the record (approach A below). |

### Out of scope

Importing past Backblaze folders, bidder-facing review status in the extension, editing or re-posting Slack
messages after review, auto-approval, reviewing individual screenshots (the decision is per bid), deleting bids.

## 2. Approach

**A (chosen): the upload request creates the record.** The extension already asks `POST /ext/upload-url` for a
signed link per screenshot, and the backend already builds the job folder key from the bidder, date, profile and
job. The same request now also carries the job URL and page title; the backend upserts the bid for that bidder
and job folder and attaches the screenshot. After the PUT succeeds the extension confirms it. The backend is the
only place that groups screenshots into bids, so a bidder can't create bids outside their own folder.

Rejected: **B**, a separate "bid" event from the extension (the extension decides grouping; a lost event leaves
screenshots without a record); **C**, building records by scanning Backblaze (no reliable job URL, delayed,
constant listing).

## 3. Data model

New collection `bids` (model `Bid`, `app/models/bid.py`):

| Field | Meaning |
|---|---|
| `bidderId`, `ownerId` | The bidder and their owner (`createdBy` at creation; used for review access). |
| `jobKey` | The job folder: `<folder>/<YYYY-MM-DD>/<profile>/<job>` (the screenshot key without its file name). Unique with `bidderId`. |
| `jobUrl`, `jobTitle` | From the extension. The first non-empty value is kept; later screenshots fill them only if still empty. |
| `firstAt` | Earliest `capturedAt` of its screenshots (UTC). Decides the bid's report day. |
| `submittedAt` | `capturedAt` of the first `submit` screenshot; `None` = "No submit screenshot". |
| `screenshots` | `[{key, step, trigger, capturedAt, uploadedAt}]`; `uploadedAt` is `None` until confirmed. |
| `status` | `pending` \| `approved` \| `rejected` (default `pending`). |
| `reviewedBy`, `reviewedByName`, `reviewedAt`, `note` | Last decision; `note` is an optional reject reason. |
| `changedSinceReview` | `True` when a screenshot is added after a decision ("New screenshots since review"). |

Indexes: unique `(bidderId, jobKey)`; `(ownerId, firstAt)`; `(bidderId, firstAt)`; `(status, firstAt)`.

## 4. Extension and `/ext` API

1. **Upload Screenshot** calls `POST /ext/upload-url` as today, with two new optional fields: `jobUrl`
   (max 2,000 chars, http/https) and `jobTitle` (max 300). The backend builds the key as today, then upserts the
   bid on `(bidderId, jobKey)`: `$setOnInsert` the identity and `pending`; `$min firstAt`; `$push` the screenshot
   (`uploadedAt: None`) unless that key is already listed (a retried request); fills `jobUrl`/`jobTitle` if empty;
   sets `submittedAt` for a `submit` trigger if empty; sets `changedSinceReview` if the bid was already reviewed.
   The response adds `bidId`.
2. The extension PUTs the PNG to the signed link (unchanged).
3. On success it calls `POST /ext/uploads/confirm` with `{key}`; the backend sets that screenshot's `uploadedAt`
   (only for a key in one of this bidder's bids; otherwise 404). A failed PUT is never confirmed, so the reviewer
   sees "Missing upload" instead of a broken image.
4. Extension changes: send `jobUrl`/`jobTitle` (it already tracks both per job record), call confirm after a
   successful PUT, bump the version. Failures of the confirm call are logged and do not block the next screenshot.

## 5. Review in the app

### API (owner of the bidder, or admin; anyone else gets 404 like other bidder routes)

- `GET /bids?day=YYYY-MM-DD&bidderId=&status=` — the bids whose `firstAt` falls in that day's **report window**
  (the existing daily window from `BID_DAILY_REPORT_TIME` in `BID_REPORT_TIMEZONE`; `day` is its period key),
  newest first, with per-bidder and total counts by status and, per bidder, the number of job folders in Backblaze
  for that window that have no bid record (see §7).
- `GET /bids/{id}/screenshots` — short-lived signed GET links (5 minutes) for its uploaded screenshots, in step
  order.
- `PUT /bids/{id}/review` — `{status: approved|rejected|pending, note?}`; records the reviewer and time, clears
  `changedSinceReview`, then refreshes that day's stored report rows for the bidder (§6).
- `POST /bids/review` — `{ids: [...], status}` for **Approve all shown** (max 500; each id access-checked).

### Page "Bid review" (`/bids/review`, linked from Bidders)

- Filters: day (report day, default today), bidder (owners see their bidders, admins all), status (default
  Pending).
- Summary: "Approved N · Pending M · Rejected K" per bidder and total, and the "folders without a record" warning.
- Rows: time (`firstAt`, report timezone), bidder, job title linking to `jobUrl` (title only if no URL),
  screenshot count, flags **No submit screenshot**, **Missing upload**, **New screenshots since review**.
- Expanding a row loads its screenshots (thumbnails, click for full size).
- Actions: **Approve** / **Reject** (optional note) per row, **Approve all shown**; keys `j`/`k` move, `a`/`r`
  approve/reject the current row. Decisions can be changed.

## 6. Counting

- `BID_RECORDS_START` (date, report timezone) is set on rollout day. For a window that **starts on or after** it,
  a bidder's count = approved bids with `firstAt` in the window. For windows before it, the folder count is used
  as today. A window that starts before it, including one that straddles the start date, keeps the folder count.
  So the first day counted from records is the first full daily window starting on or after it, and the first weekly
  window counted from records is the first one starting on or after it. Live counts judge each open window by its own
  start.
- `BidReportRow` gains `pending` and `rejected` (both `Optional[int]`); `count` stays the approved number and
  `amount = count × rate`.
- Applies to: daily/weekly Slack reports (`build_rows`), the per-bidder reports page, and live counts on the
  Bidders page (`live_counts`; records are counted in Mongo, so the Backblaze listing cache is not needed for
  them).
- Slack messages show "Approved N (pending M, rejected K)". They are posted at the cutoff as today and are not
  edited later; most of that day is still pending then.
- A review refreshes the stored `bidreports` rows (daily and weekly) covering that bid's `firstAt` for that
  bidder, so the reports page always shows current numbers.

## 7. Edge cases

- **Same job again**: a later day is a different `jobKey` (the date is in it), so a new bid; the same day is the
  same bid.
- **Screenshot after review**: decision kept, `changedSinceReview` set, row flagged.
- **Upload never confirmed**: screenshot shown as missing; the bid can still be approved.
- **Two reviewers**: last write wins; the record shows who and when.
- **Archived bidder**: records kept and reviewable; reports skip archived bidders as today.
- **Old key-based extension still in use**: its screenshots reach Backblaze without a record and count 0. The
  review page shows, per bidder per day, how many job folders in Backblaze have no bid record (folder count minus
  bids, using the existing folder listing), so a bidder still on the old extension stands out.
- **Clock skew**: `capturedAt` more than 24 hours from server time is refused as today.

## 8. Rollout

1. Release `feat/bidder-login` with this feature: backend to `dev` (Railway deploy approved by the owner),
   frontend to `main`, the extension as a new version zip.
2. Set `BID_RECORDS_START` to the rollout day (before bidders start that day) on the API service, which also runs
   the report cron. Distribute only extension 3.1.0 (3.0.0 does not confirm uploads, so its bids' screenshots would
   show as missing).
3. Owners send invite codes; bidders remove the old extension, install the new one and register
   (`docs/bids.md` rollout section).
4. Revoke each old Backblaze upload key once its bidder has registered.

## 9. Testing

- Backend unit tests (mongomock-motor, like `tests_unit/test_bids_db.py`): one bid per job and day across
  several screenshots; retried upload-url doesn't duplicate a screenshot; confirm marks `uploadedAt` and rejects
  other bidders' keys; submit sets `submittedAt`; `changedSinceReview` after a decision; review access (owner,
  admin, other user → 404); bulk review; counts use approved records from `BID_RECORDS_START` and folders before
  it; report rows refresh after a review; the folders-without-record number.
- Frontend: `tsc -b` and a Vite build; the review page is checked by the user (needs a login).
- Extension: manual run against a local backend (`serverUrl` in `chrome.storage.local`).
