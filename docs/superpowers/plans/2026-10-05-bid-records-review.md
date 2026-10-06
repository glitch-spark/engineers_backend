# Bid Records and Daily Review Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Each application recorded by the Bid Track extension becomes a reviewable bid record, and reports count approved bids instead of Backblaze job folders.

**Architecture:** `POST /ext/upload-url` upserts a `Bid` per (bidder, job folder) and attaches each screenshot; the extension confirms each successful PUT. A `bids` router serves the daily review (list, signed screenshot links, approve/reject). Report rows and live counts read approved records for windows after `BID_RECORDS_START`, folders before it.

**Tech Stack:** FastAPI + Beanie/Mongo (backend, `engineers_backend`), React + TS + SWR (frontend, `engineers_app`), Chrome MV3 extension (`Bid Track (BackBlaze)`).

**Spec:** `docs/superpowers/specs/2026-10-05-bid-records-review-design.md`

## Global Constraints

- Work on `feat/bidder-login` in `engineers_backend` and `engineers_app`, and on `main` in the extension repo. Commit per task; do not push.
- One bid per `(bidderId, jobKey)`; `jobKey` = the upload key without its file name (`<folder>/<YYYY-MM-DD>/<profile>/<job>`).
- Status values: `pending` | `approved` | `rejected` (default `pending`).
- Reviewers: the bidder's owner (`Bidder.createdBy`) or a user with `role == "admin"`; anyone else gets 404 `bid not found`.
- Counts: only `approved` counts toward `count` and `amount = round(count * rate, 2)`; pending/rejected are reported separately.
- `BID_RECORDS_START`: optional `YYYY-MM-DD` in `BID_REPORT_TIMEZONE`. A window uses records iff the setting is set and `window_end > local midnight of BID_RECORDS_START` (converted to UTC); otherwise folders. Unset = today's behavior everywhere.
- Signed view links expire in 300 seconds. Bulk review takes at most 500 ids.
- `jobUrl` max 2,000 chars and must be http(s) (else stored as `None`); `jobTitle` max 300 (truncated).
- Backend tests: `pytest tests_unit --continue-on-collection-errors` (3 files fail to collect beforehand; `test_bidder_ext.py::test_upload_url_without_storage_is_503` fails beforehand when the local `.env` configures storage). Use mongomock-motor like `tests_unit/test_bids_db.py`.
- Frontend checks: `npx tsc -b` (2 pre-existing errors in `InterviewPrepLibrary.tsx` only) and `npx vite build --outDir <scratch>`.

## Review Focus

- A `jobUrl` like `javascript:alert(1)` or a 5,000-char URL from the extension: stored as `None` / refused at validation, never rendered as a link. (Task 2 test)
- **Approve all shown** with an id belonging to another owner's bidder: that id is skipped, the rest are updated, response `{updated, skipped}`. (Task 4 test)
- Screenshots requested for a bid whose upload was never confirmed: only confirmed screenshots get links; unconfirmed ones come back with `url: null`. (Task 4 test)
- `GET /bids?day=2026-13-40` or a missing day: 400 `invalid day`, not a 500. (Task 4 test)
- `BID_RECORDS_START` unset: reports, live counts and Slack are unchanged (folder counts, no pending/rejected numbers). (Task 3 test)

---

### Task 1: Bid model and record store

**Files:**
- Create: `app/models/bid.py`, `app/bids/records.py`
- Modify: `app/models/__init__.py`, `app/db.py` (register `Bid`)
- Test: `tests_unit/test_bids_records.py`

**Interfaces:**
- Produces: `Bid` (fields per spec §3; collection `bids`; `keep_nulls = False`; indexes: unique `(bidderId, jobKey)`, `(ownerId, firstAt)`, `(bidderId, firstAt)`, `(status, firstAt)`); `Screenshot` (`key: str, step: int, trigger: str, capturedAt: datetime, uploadedAt: Optional[datetime] = None`).
- Produces: `async record_screenshot(bidder: Bidder, key: str, step: int, trigger: str, captured_at: datetime, job_url: Optional[str], job_title: Optional[str]) -> Bid` and `async confirm_upload(bidder_id: PydanticObjectId, key: str) -> bool` in `app/bids/records.py`; `job_key(key: str) -> str` (key up to its last `/`).

- [ ] **Step 1: Write the failing tests** in `tests_unit/test_bids_records.py`:
  - `test_screenshots_of_one_job_make_one_bid`: three keys under the same job folder (steps 1–3, last trigger `submit`) → one `Bid`, 3 screenshots, `firstAt` = earliest `capturedAt` (UTC), `submittedAt` = the submit screenshot's time, `status == "pending"`, `ownerId == bidder.createdBy`.
  - `test_a_retried_request_does_not_duplicate_a_screenshot`: same key twice → 1 screenshot.
  - `test_first_job_url_and_title_are_kept`: first call with `None`, second with `"https://a.io/j/1"`/`"Engineer"`, third with another URL → URL/title from the second call.
  - `test_a_screenshot_after_review_flags_the_bid`: set `status="approved"`, record a new key → status stays `approved`, `changedSinceReview is True`.
  - `test_confirm_marks_only_this_bidders_key`: `confirm_upload(bidder.id, key)` → True and `uploadedAt` set; with another bidder's id → False.
- [ ] **Step 2: Run** `pytest tests_unit/test_bids_records.py -v` — Expected: FAIL (`ModuleNotFoundError: app.models.bid`).
- [ ] **Step 3: Implement** the model and `records.py`. `record_screenshot` is one `find_one_and_update` upsert on `{bidderId, jobKey}`: `$setOnInsert` (ownerId, jobKey, status `pending`, createdAt), `$min firstAt`, `$set updatedAt`; then a second conditional `update_one` that `$push`es the screenshot only when `"screenshots.key" != key`, fills `jobUrl`/`jobTitle` with `$set` filtered on the field being null, sets `submittedAt` for `trigger == "submit"` when null, and sets `changedSinceReview: True` when `status != "pending"` and a screenshot was pushed. Convert `captured_at` to naive UTC like other models.
- [ ] **Step 4: Run** the same command — Expected: 5 passed.
- [ ] **Step 5: Commit** `feat(bids): bid records grouped per job folder`.

### Task 2: Extension API creates records and confirms uploads

**Files:**
- Modify: `app/routers/bidder_ext.py` (`UploadUrlIn`, `upload_url`, new `confirm_upload` route)
- Test: `tests_unit/test_bidder_ext.py` (add tests in its existing style)

**Interfaces:**
- Consumes: `record_screenshot`, `confirm_upload` (Task 1).
- Produces: `UploadUrlIn.jobUrl: Optional[str]` (max 2,000), `UploadUrlIn.jobTitle: Optional[str]` (max 300); response adds `"bidId": str`; `POST /ext/uploads/confirm` body `{"key": str}` → `{"ok": true}` or 404 `{"error": "upload not found"}`.

- [ ] **Step 1: Write the failing tests:**
  - `test_upload_url_records_the_bid`: POST with `jobUrl`/`jobTitle` → 200, `bidId` present, one `Bid` with the URL, title and one unconfirmed screenshot.
  - `test_upload_url_drops_a_non_http_job_url`: `jobUrl="javascript:alert(1)"` → 200, `Bid.jobUrl is None`; a 2,001-char URL → 400 with the `{"error": ...}` body.
  - `test_confirm_sets_uploaded_at` and `test_confirm_of_an_unknown_key_is_404`.
  - `test_upload_url_without_job_fields_still_works` (old extension payload) → 200.
- [ ] **Step 2: Run** `pytest tests_unit/test_bidder_ext.py -v` — Expected: the new tests FAIL.
- [ ] **Step 3: Implement.** Call `record_screenshot` after `build_upload_key` succeeds and before signing; keep the 503/422 paths unchanged. A non-http(s) `jobUrl` becomes `None` (follow the existing "routes check rules, schemas only strip/cap" convention from commit `9a03d1e`).
- [ ] **Step 4: Run** the same command — Expected: new tests pass; only the known storage-dependent test may fail.
- [ ] **Step 5: Commit** `feat(bids): record each upload as a bid and confirm finished uploads`.

### Task 3: Count approved bids in reports and live counts

**Files:**
- Modify: `app/config.py` (`BID_RECORDS_START: Optional[str] = None`, validated as `YYYY-MM-DD`), `app/models/bid_report.py` (`BidReportRow.pending`, `.rejected`: `Optional[int] = None`), `app/bids/records.py`, `app/bids/reports.py` (`build_rows`), `app/bids/live_counts.py`, `app/bids/schemas.py` (`report_out` adds `pending`, `rejected`), `app/bids/slack_format.py`, `.env.example`, `docs/bids.md`
- Test: `tests_unit/test_bids_records.py`, `tests_unit/test_bids_reports.py`, `tests_unit/test_bids_slack_format.py`

**Interfaces:**
- Produces in `app/bids/records.py`: `records_start_utc(sched: BidSchedule, s: Settings | None = None) -> Optional[datetime]`; `uses_records(end: datetime, sched) -> bool`; `async count_by_status(bidder_ids: list[PydanticObjectId], start: datetime, end: datetime) -> dict[str, tuple[int, int, int]]` (approved, pending, rejected per bidder id, zeros included); `async refresh_report_rows(bid: Bid) -> None` (recounts and updates the stored daily and weekly `BidReport` rows of that bidder whose `[periodStart, periodEnd)` contains `bid.firstAt`).
- Consumes: `window_for`, `latest_cutoff`, `open_window`, `BidSchedule` from `app/bids/windows.py`.

- [ ] **Step 1: Write the failing tests:**
  - `test_count_by_status_counts_each_status_in_the_window` (bids inside/outside `[start, end)`).
  - `test_uses_records_only_after_the_start_day`: unset → False; set to `2026-10-06` in `America/Chicago` → a window ending `2026-10-06T04:59Z` is False, ending `05:01Z` is True.
  - `test_build_rows_counts_approved_records_after_start`: 2 approved + 1 pending + 1 rejected → `count == 2`, `pending == 1`, `rejected == 1`, `amount == round(2 * rate, 2)`, storage not called.
  - `test_build_rows_uses_folders_before_start_or_when_unset` (existing fake storage; `pending is None`).
  - `test_refresh_report_rows_updates_the_stored_day` after approving a bid.
  - Slack: `test_report_line_shows_pending_and_rejected` → line contains `2 bids` and `(pending 1, rejected 1)`; rows with `pending is None` keep today's text exactly.
- [ ] **Step 2: Run** `pytest tests_unit/test_bids_records.py tests_unit/test_bids_reports.py tests_unit/test_bids_slack_format.py -v` — Expected: new tests FAIL.
- [ ] **Step 3: Implement.** `build_rows` splits bidders by `uses_records(end, sched)` (pass `sched` in; update `_run_kind`'s call) and counts records with one `count_by_status` aggregation; `live_counts` does the same per window before falling back to the folder path; the Slack line appends ` (pending M, rejected K)` only when `pending is not None`. Document `BID_RECORDS_START` in `.env.example` and in the `docs/bids.md` rollout section (set it before bidders start on rollout day).
- [ ] **Step 4: Run** the same command plus `pytest tests_unit/test_bids_live_counts*.py tests_unit/test_bids_windows.py -v` — Expected: all pass.
- [ ] **Step 5: Commit** `feat(bids): count approved bid records from BID_RECORDS_START`.

### Task 4: Review API

**Files:**
- Create: `app/routers/bids.py` (prefix `/bids`), `tests_unit/test_bids_review.py`
- Modify: `app/main.py` (include router), `app/bids/storage.py` (`presign_get(bucket, key, expires=300) -> str`, same error handling as `presign_put`)

**Interfaces:**
- Consumes: `Bid`, `refresh_report_rows`, `count_by_status` (Tasks 1, 3); `count_folders` and `get_bid_storage` from `app/bids/storage.py`; `window_for`, `schedule_from_settings`.
- Produces:
  - `GET /bids?day=YYYY-MM-DD&bidderId=&status=` → `{"day", "start", "end", "bids": [BidOut], "summary": {bidderId: {"name", "approved", "pending", "rejected", "foldersWithoutRecord": int|null}}}`; `day` is the daily report period key and defaults to the open daily window (`open_window("daily", now, sched)`) when omitted; window = `window_for("daily", cutoff, sched)` with `cutoff = combine(day, sched.daily_at, sched.tz)`; owners see their bidders, admins all non-archived and archived bidders with bids that day.
  - `BidOut`: `id, bidderId, bidderName, jobUrl, jobTitle, firstAt, submittedAt, screenshotCount, missingUploads, status, reviewedByName, reviewedAt, note, changedSinceReview` (times as ISO UTC with `Z`).
  - `GET /bids/{id}/screenshots` → `[{"key", "step", "trigger", "capturedAt", "url": str|null}]` in step order.
  - `PUT /bids/{id}/review` body `{"status": "approved"|"rejected"|"pending", "note": Optional[str] (max 500)}` → `BidOut`; clears `changedSinceReview`, sets reviewer fields, calls `refresh_report_rows`.
  - `POST /bids/review` body `{"ids": list[str] (1..500), "status": ...}` → `{"updated": int, "skipped": int}`.
  - `foldersWithoutRecord = max(0, folder_count - bids_that_day)` for that bidder; `null` when the folder can't be read.

- [ ] **Step 1: Write the failing tests** (mongomock, app with the router, `get_current_user` overridden):
  - `test_owner_lists_their_bids_for_the_day` (summary numbers, a bid from the next window excluded).
  - `test_admin_sees_every_bidder` and `test_other_user_gets_404_on_review`.
  - `test_review_sets_status_and_refreshes_reports` (stored `BidReport` row count changes 0→1).
  - `test_bulk_review_skips_foreign_ids` → `{"updated": 2, "skipped": 1}`.
  - `test_screenshots_only_sign_uploaded_ones` (fake storage `presign_get`; unconfirmed → `url is None`).
  - `test_invalid_day_is_400` and `test_day_defaults_to_the_open_window` (response `day` = that window's period key).
- [ ] **Step 2: Run** `pytest tests_unit/test_bids_review.py -v` — Expected: FAIL (`app.routers.bids` missing).
- [ ] **Step 3: Implement** the router and `presign_get`. Access guard `_reviewable(bidder, user)`: `bidder is not None and (bidder.createdBy == user.id or user.role == "admin")`, else 404 `bid not found`.
- [ ] **Step 4: Run** the same command, then the full `pytest tests_unit --continue-on-collection-errors` — Expected: new tests pass; only the known pre-existing failures remain.
- [ ] **Step 5: Commit** `feat(bids): daily bid review API`.

### Task 5: Review page and report numbers (frontend, `engineers_app`)

**Files:**
- Create: `src/pages/BidReview.tsx`
- Modify: `src/api/endpoints.ts` (types `BidReviewItem`, `BidDay`, `BidScreenshot`; `listBids(params)`, `bidScreenshots(id)`, `reviewBid(id, body)`, `reviewBids(body)`; report type adds `pending?: number | null; rejected?: number | null`), `src/App.tsx` (route `/bids/review`, `Protected`), `src/pages/Bidders.tsx` (link "Review bids"; report rows show "pending · rejected" when present)

**Interfaces:**
- Consumes: the Task 4 API exactly as listed there.

- [ ] **Step 1: Implement** the page per spec §5: query-string state (`day`, `bidder`, `status` default `pending`; with no `day`, call `GET /bids` without it and show the returned `day`); summary bar; rows with flags; expand to load screenshots (thumbnails, click opens full size in a new tab); Approve / Reject (note prompt) / Approve all shown; keys `j`/`k`/`a`/`r` when focus isn't in an input; SWR revalidate after each action.
- [ ] **Step 2: Run** `npx tsc -b` — Expected: only the 2 known `InterviewPrepLibrary.tsx` errors.
- [ ] **Step 3: Run** `npx vite build --outDir <scratch>/fe-build --emptyOutDir` — Expected: `✓ built`.
- [ ] **Step 4: Commit** `feat(bids): daily bid review page`.

### Task 6: Extension sends job info and confirms uploads (`Bid Track (BackBlaze)`)

**Files:**
- Modify: `background.js` (`getUploadUrl` payload, `uploadScreenshot`), `manifest.json` (`"version": "3.1.0"`), `README.md` (one line: each application is recorded as a bid your manager reviews)

**Interfaces:**
- Consumes: `POST /ext/upload-url` fields `jobUrl`, `jobTitle` and `POST /ext/uploads/confirm` (Task 2).

- [ ] **Step 1: Implement.** `uploadScreenshot` receives the job record's `url` and `title` (the caller already has `record`) and sends them as `jobUrl`/`jobTitle`; after a successful PUT, POST `{key}` to `/ext/uploads/confirm` through the same authenticated API helper as `getUploadUrl`; a failed confirm is logged with `console.warn` and does not change the returned `{ok: true}`.
- [ ] **Step 2: Manual check** against the local backend (`chrome.storage.local.set({ serverUrl: "http://localhost:8080" })`, local backend started with `RESUME_QUEUE_DISABLED=1 JOB_APPLY_WORKER_DISABLED=1`): register a test bidder, take step + submit screenshots on one job page → one bid in `GET /bids` with the job URL, 2 screenshots, both confirmed, `submittedAt` set. Hand the in-browser check to the user if no test bidder/invite is available.
- [ ] **Step 3: Commit** `feat: send the job page and confirm uploads so each application is a reviewable bid`.
