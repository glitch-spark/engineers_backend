# One Bidders Page Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `/bidders` becomes one page with a team week view and a bidder view (week days, their bids → focus viewer, history), replacing `/bids/review` and the history side panel.

**Architecture:** A backend `app/bids/week.py` computes pay weeks and per-bidder day/week counts for a new `GET /bids/week`; `GET /bids` gains a `week` filter. The frontend splits `Bidders.tsx` into a team view and a bidder view (small components), reuses the review row/reject pieces and the focus viewer (now scoped by day or week and able to start at a bid).

**Tech Stack:** FastAPI + Beanie (backend), React + TS + SWR + Tailwind (frontend).

**Spec:** `docs/superpowers/specs/2026-10-06-bidders-page-merge-design.md`

## Global Constraints

- Work in the existing worktrees on branch `feat/bid-review-ux`: backend `engineers_backend/.worktrees/bid-review-ux`, frontend `engineers_app/.worktrees/bid-review-ux` (node_modules is a junction — don't npm install). Never touch the main checkouts. Commit per task; never push.
- Pay week containing a date `D` (YYYY-MM-DD, report timezone): the weekly report window `[start, end)` (`window_for("weekly", cutoff, sched)`) whose end is the first weekly cutoff strictly after `D` 12:00 local. Default (no `week`): the pay week containing now. Label: `"Week ending " + end local date as "Sat Oct 10"`.
- Days of a pay week: the 7 daily windows whose **start** is in `[week.start, week.end)`, ordered, keyed by the period key of each window's end cutoff (Sun … Sat with the defaults). A day `isToday` iff now ∈ `[start, end)`; `isFuture` iff `start > now`.
- Week totals and pay come from the pay-week window itself (`count_by_status` over `[week.start, week.end)`), `pay = round(approved * rate, 2)`, `toReview = pending`. Day and week `source` = `"records"` iff `uses_records(window_start, sched)`, else `"folders"` (folder counts from `count_folder_windows`, approved = folder count, pending/rejected = null).
- URL state on `/bidders`: `week`, `bidder`, `day`, `tab` (`history`), `mode=focus`, `bid`. `/bids/review?day=&bidder=` redirects to `/bidders?bidder=<bidder>&week=<day>&day=<day>` (no bidder → team view with `week=<day>`).
- Copy: badges **Submitted** (green) / **Not marked submitted** (amber, tooltip "No screenshot was taken with Submit screenshot — the application may not have been submitted"); folder days tooltip "Folder count, not reviewed".
- Backend tests: `C:/Users/Administrator/Documents/Engineer/engineers_backend/.venv/Scripts/python.exe -m pytest tests_unit --continue-on-collection-errors` (3 known collection errors), mongomock + fake storage only. Frontend: `npx tsc -b` (2 known `InterviewPrepLibrary.tsx` errors) and `npx vite build --outDir <scratch>/fe-build --emptyOutDir`.

## Review Focus

- `week=2026-10-10` (a Saturday) → the week ending Sat Oct 10 16:00 Chicago, not the next one. (Task 1 test)
- A week across the Nov 1 DST change (`week=2026-11-04`) → 7 days keyed Sun Nov 1 … Sat Nov 7, the first one 25 h long. (Task 1 test)
- One bidder's folder listing fails on folder days → that bidder's days carry `error` and null counts; other bidders are counted. (Task 1 test)
- Clicking an already-decided bid row opens the focus viewer at that bid (not at the first pending one). (Task 4 check in the browser run)
- An old `/bids/review?day=2026-10-06&bidder=X` link lands on X's bidder view, week containing Oct 6, day Oct 6 expanded. (Task 3 check in the browser run)

---

### Task 1: Pay weeks and the week endpoint (backend)

**Files:**
- Create: `app/bids/week.py`, `tests_unit/test_bids_week.py`
- Modify: `app/routers/bids.py` (`GET /bids/week` declared before `/{bid_id}` routes; `week` filter on `list_bids`)

**Interfaces:**
- Produces in `app/bids/week.py`: `@dataclass PayWeek(start: datetime, end: datetime, cutoff: datetime, days: list[DayWindow])`, `@dataclass DayWindow(key: str, start: datetime, end: datetime)` (UTC-aware); `pay_week(sched: BidSchedule, now: datetime, week: Optional[date] = None) -> PayWeek`; `async week_board(bidders: list[Bidder], pw: PayWeek, now: datetime, sched, storage) -> dict` returning the spec §3 response (`week`, `bidders`, `totals`).
- Produces: `GET /bids/week?week=YYYY-MM-DD&includeArchived=false` (owner sees own bidders, admin all; invalid week → 400 `invalid week`); `GET /bids?week=YYYY-MM-DD` filters `firstAt` in the pay week (`day` + `week` → 400 `use day or week, not both`).

- [ ] **Step 1: Write the failing tests** in `tests_unit/test_bids_week.py` (Chicago, default schedule, fixed `now`):
  - `test_pay_week_for_a_saturday` (`week=2026-10-10` → start `2026-10-03T21:00Z`, end `2026-10-10T21:00Z`; day keys `2026-10-04` … `2026-10-10`; first day start `2026-10-03T23:30Z`).
  - `test_pay_week_default_is_the_week_containing_now` (now Sat Oct 10 17:00 Chicago → week ending Oct 17).
  - `test_pay_week_across_dst` (`week=2026-11-04` → start `2026-10-31T21:00Z`, end `2026-11-07T22:00Z`; keys `2026-11-01` … `2026-11-07`; the `2026-11-01` day runs `2026-10-31T23:30Z` → `2026-11-02T00:30Z`, 25 h; the others 24 h).
  - `test_week_board_counts_days_and_week_from_records` (bids approved/pending/rejected spread over days; per-day counts; week totals from the pay window, `pay`, `toReview`; Saturday's bid after 16:00 counted in its day but not in the week).
  - `test_week_board_folder_days_before_records_start` (fake storage; days before `BID_RECORDS_START` → `source: "folders"`, approved = folder count, pending/rejected null).
  - `test_week_board_storage_error_is_per_bidder` (one bidder's listing raises → its folder days have `error`, the other bidder's counts present).
  - `test_week_board_marks_today_and_future`, `test_week_endpoint_access` (owner/admin/other), `test_week_endpoint_include_archived`, `test_invalid_week_is_400`, `test_list_bids_week_filter`, `test_list_bids_day_and_week_is_400`.
- [ ] **Step 2: Run** `pytest tests_unit/test_bids_week.py -v` — Expected: FAIL (`app.bids.week` missing).
- [ ] **Step 3: Implement** `app/bids/week.py` and the router changes; one `Bid` aggregation per request (group by bidder × day window × status) plus one `count_by_status` for the week window; folder counts via one `count_folder_windows` call covering the folder-sourced windows.
- [ ] **Step 4: Run** the same command, then the full unit suite — Expected: pass (only the known collection errors).
- [ ] **Step 5: Commit** `feat(bids): pay-week board endpoint and week filter`.

### Task 2: Week navigator, team view, page shell and redirect (frontend)

**Files:**
- Create: `src/components/bidders/WeekNav.tsx`, `src/components/bidders/SummaryTiles.tsx`, `src/components/bidders/TeamWeekTable.tsx`, `src/components/bidders/BidderActions.tsx` (⋯ menu + edit/invite/reset/archive dialogs moved out of `Bidders.tsx`)
- Modify: `src/api/endpoints.ts` (`BidWeek` types, `bidsWeek({week?, includeArchived?})`, `listBids` accepts `week`), `src/pages/Bidders.tsx` (shell: header, Add bidder, Show archived, `WeekNav`, team view when no `bidder`), `src/App.tsx` (`/bids/review` → `<Navigate>` per Global Constraints; keep `BidReview` page file for Task 3 until removed there)

**Interfaces:**
- Consumes: Task 1 endpoint shapes.
- Produces: `WeekNav({ week, label, onChange })`; `SummaryTiles({ tiles: {label, value, hint?, action?}[] })`; `TeamWeekTable({ data: BidWeek, onOpenBidder(id, day?), onReview(bidderId?) })`; `BidderActions({ bidder, onChanged })`.

- [ ] **Step 1: Implement** per spec §4 Team view: tiles (Approved this week + pay, To review + "Review all", Rejected, Today when current week); table columns Name, status, profile, rate, 7 day cells (approved + amber pending badge, "–" future, greyed folder days with the Global Constraints tooltip, ⓘ on the straddling last day), Week, Pay, To review, ⋯; Total row; clicks per spec. Remove the "Review bids" header link.
- [ ] **Step 2: Run** `npx tsc -b` and the Vite build — Expected: only the 2 known errors; `✓ built`.
- [ ] **Step 3: Commit** `feat(bidders): team week view`.

### Task 3: Bidder view — days, bids, history (frontend)

**Files:**
- Create: `src/components/bidders/BidderView.tsx`, `src/components/bidders/DayRow.tsx`, `src/components/bids/BidRow.tsx`, `src/components/bids/RejectDialog.tsx`, `src/components/bidders/HistoryTab.tsx`
- Modify: `src/pages/Bidders.tsx` (render `BidderView` when `bidder` set), `src/components/bids/util.ts` (submitted-badge helper if useful)
- Delete: `src/pages/BidReview.tsx`, `src/components/bidders/BidderHistoryPanel.tsx` (after moving `ReportList` into `HistoryTab`)

**Interfaces:**
- Consumes: `bidsWeek`, `listBids({week, bidderId, status})`, `reviewBid`, `reviewBids` (onlyComplete), `bidderReports(id, 'weekly')`, `REJECT_REASONS`.
- Produces: `BidRow({ bid, onOpen(bidId), onDecide(bid, status, reason?, note?) })`; `RejectDialog({ bid, onClose, onSubmit(reason, note) })`; `DayRow({ day, counts, bids, expanded, onToggle, onOpenBid, onDecide, onApproveAllComplete })`.

- [ ] **Step 1: Implement** per spec §4 Bidder view: header (← Team, name, status, profile + resume names, rate, Edit + ⋯ via `BidderActions`), tiles (Approved + pay, To review + Review pending, Rejected + most common reason, Today), tabs This week | History (`tab=history`); day rows expand (`day` param) to `BidRow`s (Submitted / Not marked submitted, Missing upload, status + reason, quick Approve/Reject); clicking a row sets `mode=focus&bid=<id>&day=<day>`; per-day "Approve all complete (N)" (onlyComplete, batches of 500); History tab lists weekly reports (week ending, approved, pending, rejected, rate, pay; folder weeks "not reviewed"; click → that week).
- [ ] **Step 2: Run** `npx tsc -b` and the Vite build — Expected as Task 2.
- [ ] **Step 3: Commit** `feat(bidders): bidder view with days, bids and history`.

### Task 4: Focus viewer scopes and entry points (frontend)

**Files:**
- Modify: `src/components/bids/FocusReview.tsx` (props), `src/pages/Bidders.tsx` (render it for `mode=focus`), `src/components/bidders/TeamWeekTable.tsx` / `BidderView.tsx` (entry points)

**Interfaces:**
- Produces: `FocusReview({ scope: { day: string } | { week: string }, bidderId?: string, startAt?: string, set: 'pending' | 'all', onExit })` — `set: 'all'` navigates all bids of the scope (decided ones too) starting at `startAt`; `set: 'pending'` keeps today's behaviour. The day/week is fixed at entry (no latching to a later window).

- [ ] **Step 1: Implement.** Bid row click → `scope {day}`, `bidderId`, `startAt`, `set 'all'`; bidder "Review pending" → `scope {week}`, `bidderId`, `set 'pending'`; team "Review all" → `scope {week}`, no bidder, `set 'pending'`; team "To review" → `scope {week}`, that bidder, `set 'pending'`. Keep keys, undo, stale check and evidence unchanged; exiting returns to the view it came from (remove `mode`, `bid`).
- [ ] **Step 2: Run** `npx tsc -b` and the Vite build — Expected as Task 2.
- [ ] **Step 3: Commit** `feat(bids): focus viewer opens at a bid and over a pay week`.

### Task 5: Browser sandbox check (controller)

- [ ] **Step 1: Run** the sandbox (`scratchpad/e2e`, in-memory DB, fake store, production blocked) on the worktrees: two bidders with bids over two days → team view counts and Total row → bidder → expand a day → click a decided bid (viewer opens at it) → change it → counts update in bidder and team views → History tab → `/bids/review?day=…&bidder=…` redirect.
- [ ] **Step 2: Look at the screenshots** of team view, bidder view and the viewer; record defects for the final review.
