# Bid Review UX Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reviewers confirm "submitted" and "right profile/resume" at a glance and decide bids quickly in a keyboard-driven focus viewer.

**Architecture:** The `/bids` API gains evidence fields (profile, resumes, tailored resume, job domain, duration), reject reasons, a single-bid read and an "only complete" bulk approve. The Bid review page gets day tabs, richer rows and a focus viewer component. The extension gets a Submit screenshot button that sends `trigger: "submit"`.

**Tech Stack:** FastAPI + Beanie (backend), React + TS + SWR + Tailwind (frontend), Chrome MV3 (extension).

**Spec:** `docs/superpowers/specs/2026-10-06-bid-review-ux-design.md`

## Global Constraints

- Work in worktrees created at execution time (superpowers:using-git-worktrees) on branch `feat/bid-review-ux`: backend from local `dev` (f8d14a4 or later), frontend from local `main`, extension from its `main` (7714fd1). The main checkouts carry another session's uncommitted edits to `app/routers/bidder_ext.py`, `app/bids/upload_keys.py` and extension files — never touch or commit them. Commit per task; never push.
- Reject reasons, exactly: `no_submission` | `wrong_profile` | `not_job_page` | `duplicate` | `incomplete` | `other`; labels *No submission*, *Wrong profile/resume*, *Not a job page*, *Duplicate*, *Incomplete*, *Other*. `other` requires a non-empty (after trim) note.
- `rejectReason` is cleared whenever status becomes `approved` or `pending`.
- "Complete" bid = `status == "pending"` and `submittedAt` set and every screenshot has `uploadedAt`.
- `GET /bids` without `day` returns the open daily window (the window containing now); `day` = period key of that window's end cutoff.
- Keys in the focus viewer: `A` approve, `R` reject, `1`–`6` reasons, `J`/`K` next/previous, `←`/`→` steps, `F` full size, `U` undo; never while typing or while a dialog is open.
- Extension version `3.2.0`; result line for a submit capture: `✓ Submit (step N) saved · uploaded`.
- Backend tests: `pytest tests_unit --continue-on-collection-errors` (3 files fail to collect beforehand; `test_bidder_ext.py::test_upload_url_without_storage_is_503` fails beforehand when a local `.env` configures storage). mongomock only — never a real database. Frontend: `npx tsc -b` (2 pre-existing errors in `InterviewPrepLibrary.tsx`) and `npx vite build --outDir <scratch>`.

## Review Focus

- A bid with no `jobUrl`: evidence lookup must not fail; `jobDomain` and `tailoredResumeName` are `null`. (Task 1 test)
- "Approve all complete" with a bid that has `submittedAt` but one unconfirmed screenshot: skipped, not approved. (Task 2 test)
- Reject with `reason: "other"` and a whitespace-only note: 400 `{"error": "A note is required for Other"}`, nothing saved. (Task 2 test)
- Approving a bid previously rejected with a reason: `rejectReason` becomes `null`. (Task 2 test)
- Deciding in the focus viewer on a bid another reviewer changed meanwhile: no overwrite; the viewer shows the new status and asks again. (Task 4 manual check via `GET /bids/{id}`)

---

### Task 1: Evidence fields, single-bid read, open-window default

**Files:**
- Create: `app/bids/evidence.py`
- Modify: `app/routers/bids.py` (`_bid_out`, `list_bids` default day + `timezone`, `bid_screenshots`, new `GET /bids/{id}`)
- Test: `tests_unit/test_bids_review.py`, `tests_unit/test_bids_evidence.py`

**Interfaces:**
- Produces in `app/bids/evidence.py`: `job_domain(url: Optional[str]) -> Optional[str]` (host without `www.`); `duration_sec(bid: Bid) -> int`; `async evidence_for(bids: list[Bid], bidders: dict[str, Bidder]) -> dict[str, dict]` keyed by bid id → `{"profileName", "resumeNames", "tailoredResumeName"}` (one `Account` query, one `ResumeApplication` query; tailored = newest `status == "completed"` with `accountId == bidder.profileId` and `jobUrl == bid.jobUrl`, its `pdfFilename`).
- Produces in the API: `BidOut` adds `jobDomain, durationSec, profileName, resumeNames, tailoredResumeName, rejectReason` (`rejectReason` is `null` until Task 2); `GET /bids` response adds `"timezone": sched.tz.key`; screenshots add `isSubmit`; `GET /bids/{id}` → `BidOut` (same access rule, 404 `bid not found`).

- [ ] **Step 1: Write the failing tests:** in `test_bids_evidence.py`: `test_job_domain` (`https://www.boards.greenhouse.io/acme/jobs/1` → `boards.greenhouse.io`; `None` → `None`); `test_duration_sec` (screenshots at 10:00:00 and 10:04:30 → 270; one screenshot → 0); `test_evidence_for_profile_resumes_and_tailored` (profile "Ian Meron" with resumes `A.pdf`, `B.pdf`; two completed tailored jobs for the URL → newest `pdfFilename`; a failed one ignored); `test_evidence_without_profile_or_url` (no profile → all `None`/`[]`; bid without `jobUrl` → `tailoredResumeName is None`). In `test_bids_review.py`: `test_list_includes_evidence_and_timezone`, `test_get_one_bid` (owner 200, other user 404), `test_screenshots_mark_submit`, and replace the default-day tests with `test_default_day_is_the_open_window` parametrized at 2026-10-06 10:00 Chicago → day `2026-10-06` and 2026-10-06 20:00 Chicago → day `2026-10-07` (default schedule, cutoff 18:30), asserting literal day/start/end.
- [ ] **Step 2: Run** `pytest tests_unit/test_bids_evidence.py tests_unit/test_bids_review.py -v` — Expected: new tests FAIL.
- [ ] **Step 3: Implement** `app/bids/evidence.py` and the router changes; `list_bids` calls `evidence_for` once for the returned bids.
- [ ] **Step 4: Run** the same command, then the full unit suite — Expected: pass (only the known pre-existing failures).
- [ ] **Step 5: Commit** `feat(bids): review evidence — profile, resumes, tailored resume, job domain, duration`.

### Task 2: Reject reasons and "approve all complete"

**Files:**
- Modify: `app/models/bid.py` (`rejectReason: Optional[str] = None`), `app/routers/bids.py` (`ReviewIn`, `BulkReviewIn`, `_decision`, `review_bids`)
- Test: `tests_unit/test_bids_review.py`

**Interfaces:**
- Consumes: Task 1's `BidOut`.
- Produces: `PUT /bids/{id}/review` body `{status, note?, reason?}`; `POST /bids/review` body `{ids, status, reason?, onlyComplete?: bool = false}` → `{updated, skipped}`. `reason` is only accepted with `status == "rejected"` (else 400 `{"error": "A reason only goes with a rejection"}`); unknown reason → 400.

- [ ] **Step 1: Write the failing tests:** `test_reject_with_reason_is_stored_and_returned`; `test_other_needs_a_note` (blank/whitespace note → 400 `A note is required for Other`, bid unchanged); `test_approve_clears_reject_reason`; `test_pending_clears_reject_reason`; `test_reason_without_rejection_is_400`; `test_bulk_only_complete_skips_rejected_incomplete_and_unconfirmed` (4 ids: complete pending → approved; rejected; pending without `submittedAt`; pending with `submittedAt` and one unconfirmed screenshot → `{"updated": 1, "skipped": 3}`); `test_bulk_without_only_complete_unchanged` (existing behaviour kept).
- [ ] **Step 2: Run** `pytest tests_unit/test_bids_review.py -v` — Expected: new tests FAIL.
- [ ] **Step 3: Implement.** The `onlyComplete` filter is applied in the Mongo update filter (`status: "pending"`, `submittedAt: {$ne: None}`, `screenshots.uploadedAt` has no `None`), so a stale page can't widen it; report refresh as today, once per distinct (bidder, daily cutoff, weekly cutoff).
- [ ] **Step 4: Run** the same command and the full unit suite — Expected: pass.
- [ ] **Step 5: Commit** `feat(bids): reject reasons and approve-all-complete`.

### Task 3: List view — day tabs, evidence rows, approve all complete (frontend)

**Files:**
- Modify: `src/api/endpoints.ts` (types `BidReviewItem` + Task 1/2 fields, `BidDay.timezone`, `BidScreenshot.isSubmit`, `RejectReason` union + `REJECT_REASON_LABELS`, `getBid(id)`, `reviewBid(id, {status, note?, reason?})`, `reviewBids({ids, status, reason?, onlyComplete?})`), `src/pages/BidReview.tsx`

**Interfaces:**
- Consumes: Tasks 1–2 API exactly as listed in their Interfaces.
- Produces: `REJECT_REASONS: { value: RejectReason; label: string; key: '1'..'6' }[]` exported from `src/api/endpoints.ts` for Task 4.

- [ ] **Step 1: Implement.** Day tabs "Today (in progress)" (no `day` param) and "Previous day (reported)" (`day` = returned day − 1 calendar day) plus the date picker; the window line "Oct 5 6:30 PM → Oct 6 6:30 PM Chicago" formatted with `Intl.DateTimeFormat` in `timezone` (city = last segment of the tz key, `_` → space); rows show `jobDomain`, `profileName`, a Submit badge or the No-submit flag, and the reject reason label; the reject dialog gets the six reason buttons (Other requires the note); "Approve all shown" becomes **"Approve all complete (N)"** (N = shown bids that are complete per Global Constraints; sends `onlyComplete: true` in batches of 500; confirm dialog states N); a **"Review pending (N)"** button sets `mode=focus` (Task 4 renders it; until then it may link with the param only).
- [ ] **Step 2: Run** `npx tsc -b` — Expected: only the 2 known `InterviewPrepLibrary.tsx` errors.
- [ ] **Step 3: Run** `npx vite build --outDir <scratch>/fe-build --emptyOutDir` — Expected: `✓ built`.
- [ ] **Step 4: Commit** `feat(bids): review list with day tabs, evidence and approve-all-complete`.

### Task 4: Focus viewer (frontend)

**Files:**
- Create: `src/components/bids/FocusReview.tsx`
- Modify: `src/pages/BidReview.tsx` (render `FocusReview` when `mode=focus`, pass filters)

**Interfaces:**
- Consumes: Task 3's API functions and `REJECT_REASONS`; `bidScreenshots(id)` with `isSubmit`.
- Produces: `FocusReview({ day, bidderId, onExit })` default export.

- [ ] **Step 1: Implement** per spec §4: pending bids of the filtered day oldest first; left = Submit screenshot (else last + "No submit screenshot" banner) with the step strip, "Screenshot didn't upload" for unconfirmed slots; right panel = bidder, profile, resume names, tailored resume (or "No profile assigned"), job title/company, `jobDomain` link, steps, duration, first time, flags, status; Approve / Reject with reason buttons (+ note for Other); auto-advance to the next pending bid; progress line "Bid X of Y pending · A approved · R rejected"; toast with Undo, `U` restores the previous `{status, reason, note}`; before a decision `getBid(id)` — if its status differs from the one shown, show "Changed by <reviewer> to <status>" and require a second press; prefetch the next bid's screenshots; done state "All bids for <day> reviewed" with a link back to the list; keyboard per Global Constraints.
- [ ] **Step 2: Run** `npx tsc -b` and the Vite build — Expected as Task 3.
- [ ] **Step 3: Commit** `feat(bids): focus viewer for one-by-one review`.

### Task 5: Submit screenshot in the extension

**Files:**
- Modify: `popup/popup.html` (button `#submit-shot` "Submit screenshot" under `#upload`), `popup/popup.js` (handler; result line), `content/content.js` (`CAPTURE_NOW` accepts `message.trigger === "submit"`, default `"step"`), `manifest.json` (`3.2.0`), `README.md` (one line: click Submit screenshot on the confirmation page after submitting, or the final page just before Submit if there is none)

**Interfaces:**
- Consumes: backend `/ext/upload-url` `trigger: "submit"` (already supported).

- [ ] **Step 1: Implement.** Share the capture path with Upload Screenshot (one function taking `trigger`); the result line for a submit capture reads `✓ Submit (step N) saved · uploaded`.
- [ ] **Step 2: Run** `node --check` on `popup/popup.js` and `content/content.js` — Expected: no output, exit 0.
- [ ] **Step 3: Commit** `feat: Submit screenshot button marks the application's submission (3.2.0)`.

### Task 6: End-to-end check in the browser sandbox

**Files:**
- Modify (scratch only, not committed): the e2e sandbox scripts under the session scratchpad `e2e/` (`e2e_backend.py`, `e2e_drive.py`, `e2e_review.py`) to point at the three worktrees.

- [ ] **Step 1: Run** the sandbox (in-memory DB, fake store; production hosts blocked) on the worktrees: extension Upload + Submit screenshot on two job pages → `GET /bids` shows `submittedAt`, `isSubmit`, evidence fields → focus viewer: approve one, reject one with *Wrong profile/resume*, undo, re-decide → list shows reasons; "Approve all complete" counts only complete pending bids.
- [ ] **Step 2: Look at the screenshots** of the focus viewer and list; record any defect as a finding for the final review.
