# Bidder Performance Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A Performance tab on the Bidders page: each bidder's weekly bids, streak of 50+ weekdays, hit rate,
average per weekday, interviews on their profile, interviews per 100 bids, last bid and change, over a Week / Month /
Custom period, with top-performer cards.

**Architecture:** A new `app/bids/performance.py` (pure helpers + loaders) behind `GET /bids/performance`. Per-day
counts reuse the pay board's `app/bids/week._day_counts` over Bid Track daily windows (one aggregation covering the
period and the previous one); interviews and the shared-profile flag reuse `app/profile_metrics`. The SPA adds a
`PerformanceTab` component and Pay week / Performance tabs on `src/pages/Bidders.tsx`.

**Tech Stack:** FastAPI + Beanie (Motor), pytest + mongomock-motor; Vite + React 18 + SWR + Tailwind.

**Spec:** `docs/superpowers/specs/2026-11-04-bidder-performance-design.md`

**Repos / branches:** `engineers_backend` and `engineers_app`, both on `feat/new-feature-3`. Backend tests:
`.venv/Scripts/python -m pytest tests_unit/<file> -v` from `engineers_backend`. Full unit run:
`.venv/Scripts/python -m pytest tests_unit -q --ignore=tests_unit/test_resume_experience_roles.py --ignore=tests_unit/test_style_serializer.py --ignore=tests_unit/test_template_annotate_removals.py`
(known failures on `dev`: `test_bidder_ext::test_upload_url_without_storage_is_503`,
`test_resume_prompts::test_apply_prompts_rejects_bad_lists[prompts5-new-1]`).

## Global Constraints

- A day is a Bid Track day: the daily window `window_for("daily", cutoff)` ending at `BID_DAILY_REPORT_TIME` in
  `BID_REPORT_TIMEZONE` (18:30 America/Chicago); a bid at the cutoff is the next day's.
- Weeks are Monday–Sunday of those days; weekly columns are every week touching the period, `partial` when cut.
- Bids = records with `status` approved or pending; rejected reported apart, never counted.
- 50+ day = a weekday (Mon–Fri) with ≥ 50 approved + pending bids; weekends neither count nor break a run.
- Weekdays counted: from max(period start, bidder creation day, `BID_RECORDS_START` day) to min(period end, archive
  day, yesterday); today added only once it has ≥ 50.
- Interviews = `profile_metrics.interview_rounds` on `Bidder.profileId`; `sharedProfile` when another active bidder
  has the profile or `profile_metrics.self_bids` has typed self bids for it in the period.
- `per100` null under 100 bids; `change` null when the previous total is 0; `hitRate` / `avgPerWeekday` null with 0
  weekdays counted.
- Rounding: `hitRate`, `change` 3 decimals; `avgPerWeekday` 1; `per100` 2.
- Previous period: Week → the week before; Month → the month before, cut to the same number of days while the current
  month runs to today; Custom → the same number of days just before.
- Visibility: `routers/bids._visible_bidders` (own bidders; admin all, `ownerId` narrows); archived only with
  `includeArchived`.
- Period parsing as Statistics: `routers.metrics._stats_days` (bad date / range → 400), `interview_rates.window` /
  `label`; unknown period → 422.
- URL key for the tab: `view=performance` (`tab` belongs to the bidder view).
- Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. A bidder created or archived mid-period → weekdays outside their active days are not misses (hit rate and streaks
   ignore them) — `test_streaks_clip_to_active_days` in Task 1.
2. A bid at 18:30:00 Chicago, and a day spanning a DST change → counted on the right report day —
   `test_day_counts_cutoff_and_dst` in Task 2.
3. Two bidders on one profile → both show its interviews, the team total counts them once, both flagged shared —
   `test_shared_profile_counted_once` in Task 2.
4. A period that started before `BID_RECORDS_START` → those days aren't misses and `recordsStart` is returned —
   `test_records_start_clips_weekdays` in Task 2.
5. The current month on its 4th day → change compares with the first 4 days of last month, not the whole month —
   `test_previous_range` in Task 1.

---

### Task 1: Pure helpers

**Files:**
- Create: `app/bids/performance.py`
- Test: `tests_unit/test_bids_performance.py`

**Interfaces:**
- Produces in `app/bids/performance.py`:
  - `TARGET = 50`, `MIN_BIDS_FOR_RATE = 100`
  - `week_columns(start: date, end: date) -> list[dict]` → `[{"start": iso, "end": iso, "partial": bool}]`, `start` /
    `end` clipped to the range, `partial` when the Mon–Sun week is cut.
  - `previous_range(period: str, start: date, end: date) -> tuple[date, date]` per Global Constraints.
  - `streaks(counts: dict[date, int], first: date, last: date, today: date) -> dict` →
    `{"longestStreak", "currentStreak", "hitDays", "weekdays"}`; `counts` maps day → approved + pending; weekdays
    counted are Mon–Fri in [first, min(last, today − 1)], plus `today` when `first ≤ today ≤ last` and
    `counts[today] ≥ TARGET`. With `first > last`: all zeros.
  - `rank(rows: list[dict]) -> dict` → `{"bids", "streak", "interviews", "conversion"}`, each the top-3 `id`s by the
    spec's §2 table (conversion only rows with `total ≥ MIN_BIDS_FOR_RATE` and `per100` not null; rows whose value is
    null or 0 are left out of that card).

- [ ] **Step 1: Write the failing tests:**
  - `test_week_columns`: `week_columns(date(2026,10,1), date(2026,10,31))` → 5 entries; first
    `{"start": "2026-10-01", "end": "2026-10-04", "partial": True}`; second `start "2026-10-05"`, `end "2026-10-11"`,
    `partial False`; last `{"start": "2026-10-26", "end": "2026-10-31", "partial": True}`.
  - `test_streaks_weekend_does_not_break`: Fri Oct 23 = 50, Mon Oct 26 = 60, Tue Oct 27 = 55 (first Oct 19, last
    Oct 30, today Nov 4) → `longestStreak 3`; Oct 19–22 = 0 → `hitDays 3`, `weekdays 10`.
  - `test_streaks_49_breaks`: first Oct 19, last Oct 23, Mon–Fri = 50, 50, 49, 50, 50 → `longestStreak 2`,
    `currentStreak 2`, `hitDays 4`, `weekdays 5`.
  - `test_streaks_today`: today Wed Nov 4, Mon–Tue 60, today 30 → `weekdays 2`, `currentStreak 2`; today 50 →
    `weekdays 3`, `currentStreak 3`.
  - `test_streaks_clip_to_active_days`: `first` = Wed Oct 21 (the bidder's creation day) → Oct 19–20 are not counted
    (`weekdays` 8 for Oct 21–30).
  - `test_previous_range`: `("week", Oct 19, Oct 25)` → `(Oct 12, Oct 18)`; `("month", Oct 1, Oct 31)` →
    `(Sep 1, Sep 30)`; `("month", Nov 1, Nov 4)` → `(Oct 1, Oct 4)`; `("month", Mar 1, Mar 31)` → `(Feb 1, Feb 28)`;
    `("custom", Sep 1, Oct 31)` → `(Jul 2, Aug 31)`.
  - `test_rank`: ties per the spec table (bids: approved then name; streak: hitRate then total; interviews: per100
    then name; conversion: ≥ 100 bids only); at most 3 ids each.

- [ ] **Step 2: Run** `.venv/Scripts/python -m pytest tests_unit/test_bids_performance.py -v` — Expected: FAIL
  (module missing).

- [ ] **Step 3: Implement** the names above. Module docstring: what the Performance tab measures and that days are
  Bid Track days (see spec §1).

- [ ] **Step 4: Run** the same command — Expected: PASS.

- [ ] **Step 5: Commit** — `feat(bids): performance helpers (weeks, streaks, previous period, ranking)`

---

### Task 2: Loaders and the response

**Files:**
- Modify: `app/bids/performance.py`
- Test: `tests_unit/test_bids_performance.py` (add a mongomock `db` fixture with `UserExtra, Account, Bidder, Bid,
  DailyPlan, Interview, InterviewQuestion`, as `tests_unit/test_bids_week.py` sets up bids)

**Interfaces:**
- Consumes: Task 1 helpers; `app.bids.week._day_counts(bidders, days: list[DayWindow])`,
  `app.bids.week.DayWindow`; `app.bids.windows.window_for`, `period_key`, `schedule_from_settings`;
  `app.bids.records.records_start_utc`; `app.profile_metrics.interview_rounds`, `self_bids`.
- Produces:
  - `day_windows(start: date, end: date, sched) -> list[DayWindow]` — one per local date, the window ending at that
    date's cutoff.
  - `async day_counts(bidders: list[Bidder], start: date, end: date, sched) -> dict[tuple[str, date], tuple[int, int, int]]`
    — wraps `_day_counts`, keyed by bidder id and date.
  - `async last_bid_at(bidder_ids: list) -> dict[str, datetime]` — one `$group` with `$max: "$firstAt"`.
  - `async performance(bidders: list[Bidder], period: str, start: date, end: date, label: str, today: date) -> dict`
    — the spec's §3 response (all keys, `weekly` aligned with `weeks`, `totals`, `top`).

- [ ] **Step 1: Write the failing tests:**
  - `test_day_counts_cutoff_and_dst`: bids at Chicago 18:29:59 and 18:30:00 on Oct 20 → days Oct 20 and Oct 21;
    DST ends Sun Nov 1, 2026 at 02:00: a bid at Sat Oct 31 18:30 CDT → day Nov 1, one at Sun Nov 1 18:29 CST → day
    Nov 1, one at Sun Nov 1 18:30 CST → day Nov 2; statuses counted apart.
  - `test_performance_counts`: two bidders, approved + pending summed, rejected only in `rejected`; `weekly` sums to
    `total`; `previousTotal` and `change` (e.g. 110 vs 100 → `0.1`); `change` None when previous is 0.
  - `test_records_start_clips_weekdays`: `BID_RECORDS_START` = Oct 15 (patch settings) with an October period →
    `recordsStart "2026-10-15"` and `weekdays` counts from Oct 15.
  - `test_shared_profile_counted_once`: bidders A and B on profile P with 2 rounds in the period → both
    `interviews 2`, both `sharedProfile True`, `totals.interviews == 2`; a lone bidder with typed self bids on its
    profile → `sharedProfile True`; a bidder without a profile → `interviews None`.
  - `test_per100_and_last_bid`: 99 bids → `per100 None`; 200 bids and 1 round → `0.5`; `lastBidAt` is the latest bid
    even when it is after the period.

- [ ] **Step 2: Run** `.venv/Scripts/python -m pytest tests_unit/test_bids_performance.py -v` — Expected: new tests
  FAIL.

- [ ] **Step 3: Implement.** `performance` calls `day_counts` once over [previous start, end], splits this period
  from the previous one, and per bidder takes `first` = max(start, creation day, records-start day) and `last` =
  min(end, archive day) in the report timezone for `streaks`. Profile names come from one `Account` find projected
  to `name`, `createdBy`. Team `per100` sums each profile's interviews once.

- [ ] **Step 4: Run** the same command — Expected: PASS.

- [ ] **Step 5: Commit** — `feat(bids): bidder performance over a period`

---

### Task 3: Endpoint

**Files:**
- Modify: `app/routers/bids.py` (route declared before `@router.get("/{bid_id}")`)
- Test: `tests_unit/test_bids_performance_router.py`

**Interfaces:**
- Consumes: `performance` (Task 2); `_visible_bidders`, `_now`; `routers.metrics._stats_days`;
  `interview_rates.window`, `label`.
- Produces: `GET /bids/performance` → `bids_performance(user, period: str = Query("month"), date: str = Query(""),
  to: str = Query(""), includeArchived: bool = False, ownerId: Optional[str] = None) -> dict`. "Today" is the
  report-timezone date of `_now()`; the current week or month ends at today.

- [ ] **Step 1: Write the failing tests** (route called directly, every Query argument passed):
  `test_shape` (top-level keys `period, start, end, label, recordsStart, weeks, bidders, totals, top`);
  `test_visibility` (staff sees own bidders only; admin sees all, `ownerId` narrows; archived only with
  `includeArchived=True`); `test_current_month_ends_today`; `test_bad_period_422`; `test_bad_range_400`.

- [ ] **Step 2: Run** `.venv/Scripts/python -m pytest tests_unit/test_bids_performance_router.py -v` — Expected: FAIL.

- [ ] **Step 3: Implement** the route.

- [ ] **Step 4: Run** both performance test files, then the full unit run — Expected: only the known failures.

- [ ] **Step 5: Commit** — `feat(bids): GET /bids/performance`

---

### Task 4: Performance tab component (frontend)

**Files:**
- Modify: `src/api/endpoints.ts`
- Create: `src/components/bidders/PerformanceTab.tsx`

**Interfaces:**
- Consumes: `GET /bids/performance` (Task 3); `src/components/statistics/controls.tsx` (`PERIODS`, `Segmented`,
  `PeriodNav`, `filtersError`, `isoDay`); `fmtCount` from `src/lib/profileMetrics.ts`.
- Produces: types `BidPerformance`, `BidPerformanceRow`; `getBidPerformance(params: { period: 'week' | 'month' |
  'custom'; date?: string; to?: string; includeArchived?: boolean; ownerId?: string })`; component
  `PerformanceTab({ ownerId, includeArchived, onOpenBidder }: { ownerId: string; includeArchived: boolean;
  onOpenBidder: (id: string, week: string) => void })`.

- [ ] **Step 1: Implement** per spec §2: the filter bar (default month), the four cards (top 3, name buttons call
  `onOpenBidder(id, lastDayOfPeriod)`), the table with one column per `weeks` entry (`Oct 27*` + days tooltip when
  partial), Total / Streak / Hit rate / Avg / weekday / Interviews / Per 100 bids / Last bid columns with the spec's
  sub-lines and tooltips, sortable headers (desc first, nulls last), the Total row, and the loading / error / empty /
  records-start states. "Last bid" is relative (`2h ago`, `3d ago`) and amber when older than one weekday.

- [ ] **Step 2: Verify** `npm run lint` — Expected: exit 0.

- [ ] **Step 3: Commit** (in `engineers_app`) — `feat(bidders): Performance tab component`

---

### Task 5: Tabs on the Bidders page (frontend)

**Files:**
- Modify: `src/pages/Bidders.tsx`

**Interfaces:**
- Consumes: `PerformanceTab` (Task 4); `src/components/Tabs.tsx`.

- [ ] **Step 1: Implement.** When no bidder is open: `Tabs` with **Pay week** / **Performance** under the header,
  `view=performance` in the URL (`setParam({ view })`; add `'view'` to the `Param` type). Performance hides
  `WeekNav` and renders `PerformanceTab` with the page's owner filter and Show archived switch; `onOpenBidder` sets
  `{ bidder: id, week, view: null }`. The Pay week tab renders exactly today's content.

- [ ] **Step 2: Verify** `npm run lint` and `npm run build` — Expected: both exit 0.

- [ ] **Step 3: Commit** — `feat(bidders): Pay week and Performance tabs`

---

### Task 6: Verification

- [ ] **Step 1:** Backend full unit run — only the known failures.
- [ ] **Step 2:** Frontend `npm run lint && npm run build` — exit 0.
- [ ] **Step 3:** With the user's go-ahead, a read-only check against the live database in the scratchpad: for each
  visible bidder and an October period, recompute total, approved / pending / rejected, 50+ days and weekdays counted
  with plain per-day window queries (`bids.count_documents` per window) and compare with `performance()`; no writes
  (`init_beanie(..., skip_indexes=True)`).
- [ ] **Step 4:** Manual check in the local app: tabs, periods, cards, sorting, phone width, dark mode.
