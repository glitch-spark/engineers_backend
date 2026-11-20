# Statistics Insights — Slice 1 (Frame + Bids) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the shared "you vs team → what to work on → everyone → trend" frame and the `/stats/bids` endpoint, replacing today's Bids tab.

**Architecture:** Backend: pure frame helpers (`app/stats/frame.py`), the Bids view + rules (`app/stats/bids.py`), a router (`app/routers/stats.py`) reusing `interview_rates` loaders. App: a generic `StatsFrame` rendering the contract, a Viewing picker for admins, and the new Bids tab.

**Tech Stack:** FastAPI + Beanie (mongomock tests), React + SWR + Recharts (vitest).

**Spec:** `engineers_backend/docs/superpowers/specs/2026-11-18-stats-insights-design.md` (sections "The shared contract", "Bids", "The app (frame)", "Decisions from the builder check").

**Repos / branches:** backend `/home/eningeer/work/Engineer/engineers_backend` on `feat/stats-insights` (based on `chore/dev-fast-start`; spec commit 027c5db); app `/home/eningeer/work/Engineer/engineers_app` — create `feat/stats-insights` from `main` in Task 4. Main checkouts, no worktrees.

## Global Constraints

- Contract keys exactly as the spec: `tab, label, subject{userId,name}, headline[{key,label,value,format,team{median,p25,p75,min,max,n},rank,of,vsMedian,higherIsBetter,note}], tips[{id,severity,text,detail,evidence}], everyone{metric,rows[{userId,name,value,vsMedian,rank,parts,isSubject}]}, trend{unit,buckets[{label,you,median}]}, notEnoughData, definitions[]`.
- `severity` ∈ `bad | watch | good`; at most 4 tips, ordered bad → watch → good, then by impact.
- Team = `role != "admin"` users with activity (bids > 0) in the period/region; subject always shown; a subject outside the team gets `rank` = would-be rank and is not in the distribution.
- `notEnoughData` when team < 3 people or < 3 Mon–Fri days elapsed in the period; then `tips = []`.
- Self bids count every stored DailyPlan (no `loggedAt` filter) — `/metrics/interview-rates` keeps its logged-only rule.
- Working days Mon–Fri. Previous period = same elapsed days immediately before (month: same day range of previous month; week: same weekdays last week; custom: equal-length window before).
- `user` query param admin-only; non-admin with another user's id → 403 (as `dashboard._subject_id`).
- URL keys stay `tab, region, range, from, to` + new `user`.
- Commits: single line, no co-author / "Generated with" trailers. Never commit the app's `.env.*`. Never run anything against the real DB (backend `.env` is production); dev server runs with `DB_FAST_START=1`.
- Backend tests: `.venv/bin/python -m pytest -q -p no:cacheprovider tests_unit/<file>`; full suite with `--ignore=tests_unit/test_resume_experience_roles.py --ignore=tests_unit/test_style_serializer.py --ignore=tests_unit/test_template_annotate_removals.py` (2 known local failures). App: `npx tsc --noEmit`, `npx vitest run`.

## Review Focus

1. Admin viewing a staff member sees that person's header/tips; staff passing `user=` of someone else gets 403 (Task 3).
2. Early in a month (day 1–2) → `notEnoughData`, no tips, header still renders (Task 2 + Task 5).
3. Region filter: team, ranking and rules all within the region; profiles with no region excluded (Task 2).
4. Subject with zero bids / admin subject → "would rank N of M", not counted in median (Task 1/2).
5. Tie values share a rank; a team where everyone has the same value → vsMedian 0, no `low_self_share` noise (Task 1/2).

---

### Task 1: Frame helpers + counts option (backend)

**Files:** Create `app/stats/__init__.py`, `app/stats/frame.py`; Modify `app/interview_rates.py` (`counts` gains `logged_only: bool = True`). Test: `tests_unit/test_stats_frame.py`.

**Interfaces (produces):**
- `distribution(values: list[float]) -> dict` → `{"median","p25","p75","min","max","n"}` (linear-interpolated quartiles; empty → all None, n 0).
- `rank_of(value: float, values: list[float], higher_is_better: bool = True) -> tuple[int, int]` → (1-based rank, of); ties share the best rank; `value` not required to be in `values` (would-be rank, `of = len(values) + 1` in that case — caller passes `in_team`): signature `rank_of(value, values, higher_is_better=True, in_team=True)`.
- `vs_median(value: float, median: Optional[float]) -> Optional[float]` → `(value - median) / median`, None when median falsy.
- `working_days(start: date, end: date, today: date) -> list[date]` → Mon–Fri days in `[start, min(end, today)]`.
- `previous_window(period: str, start: date, end: date, today: date) -> tuple[date, date]` per the constraint (elapsed = `[start, min(end, today)]`).
- `trend_buckets(start: date, end: date) -> tuple[str, list[tuple[date, date, str]]]` → unit `"week"` when the span ≥ 14 days else `"day"`, buckets from `interview_rates.buckets`.
- `order_tips(tips: list[dict]) -> list[dict]` → sort bad/watch/good then `-tip["impact"]`, drop `impact`, keep first 4.

- [ ] **Step 1: Failing tests** — `test_distribution_quartiles` (`[1,2,3,4,5]` → median 3, p25 2, p75 4, min 1, max 5, n 5); `test_rank_ties_share` (`rank_of(5,[9,5,5,1])` → (2,4)); `test_rank_outside_team` (`rank_of(0,[9,5,1], in_team=False)` → (4,4)); `test_vs_median_zero_median_is_none`; `test_working_days_stop_at_today` (Nov 16–22 2026, today Wed 18 → [16,17,18]); `test_previous_window_month_same_days` (month Nov 1–30, today Nov 18 → Oct 1–18); `test_previous_window_week` (Nov 16–22, today 18 → Nov 9–11); `test_previous_window_custom` (Nov 5–11 → Oct 29–Nov 4); `test_trend_unit` (7-day span → "day", 30-day → "week"); `test_order_tips_caps_at_four`; `test_counts_logged_only_false_includes_unposted_days` (mongomock DailyPlan with `loggedAt: None`, `done.bidsSelf: 7` → counted only when `logged_only=False`).
- [ ] **Step 2: Run** — FAIL (module missing).
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the file + `tests_unit` files touching `interview_rates` (grep) — PASS.
- [ ] **Step 5: Commit** `feat: statistics frame helpers; counts can include unposted days`

---

### Task 2: Bids view + rules (backend)

**Files:** Create `app/stats/bids.py`. Test: `tests_unit/test_stats_bids.py` (mongomock; fixtures like `tests_unit/test_dashboard_week.py`).

**Interfaces:**
- Consumes Task 1 helpers; `interview_rates._profile_regions`, `interview_rates.counts(..., logged_only=False, days=...)`, `interview_rates.window`, `interview_rates.label`, `plan_auto.weekly_plans_for`, `plan_auto.day_targets`, `plan_profiles.owner_profiles`, `bids` per profile: self from `DailyPlan.done.bidsSelfByProfile`, bidder from `Bidder.profileId` via `interview_rates.bidder_bids` (extend it with an optional `by_profile` out-dict if needed).
- Produces `async def bids_view(subject: UserExtra, period: str, day: date, region: str, until: Optional[date], today: date) -> dict` returning the full contract with `tab = "bids"`.
- Produces pure rule functions (each returns a tip dict with `impact` or None): `low_self_share(me_self, me_total, team_shares)`, `weak_weekdays(per_weekday: dict[int, list[int]])`, `idle_profiles(profile_last_bid: dict[str, Optional[date]], today)`, `behind_target(actual, target)`, `up_vs_last_period(now, before)`.

Headline (in order): `bids` ("Your bids", note `Self {s} · bidder {b}`), `median` is inside `team`; `rank`; `perDay` ("Per working day", value = bids ÷ elapsed working days, note `team {median per-day} · target {target per-day}` — target omitted when 0). Everyone: metric `bids`, parts `{self, bidder}`, ordered by rank. Trend: subject's bids per bucket and the team median per bucket. Definitions: 2 lines (self bids = every saved day's typed applies; bidder bids = Bid Track approved + pending, by Bid Track day).

Rule thresholds (constants at the top): `SELF_SHARE_GAP = 0.15` (bad when subject's self share < team median share − 0.15); `WEAK_DAY_RATIO = 0.6` with ≥ 2 samples per weekday (watch, names the weekdays); `IDLE_DAYS = 7` over non-archived profiles in the region (watch, names up to 3); `BEHIND_RATIO = 0.85` (bad); `UP_RATIO = 0.15` (good). Tip text per the mockup wording, with the numbers.

- [ ] **Step 1: Failing tests** — pure rules: one positive and one negative case each, e.g. `test_low_self_share_fires_below_team_minus_15pts` (self 214/519 vs team median share 0.58 → bad, text contains "41%" and "58%"); `test_weak_weekdays_needs_two_samples`; `test_idle_profiles_ignores_archived`; `test_behind_target_skipped_when_no_target`; `test_up_vs_last_period_good`. View: `test_bids_view_contract_keys`; `test_unposted_day_self_bids_count`; `test_subject_outside_team_would_rank` (admin subject); `test_not_enough_data_early_in_month` (today Nov 2 → `notEnoughData` true, tips []); `test_region_filter_excludes_other_regions`; `test_everyone_sorted_with_subject_flag`.
- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the file — PASS.
- [ ] **Step 5: Commit** `feat: statistics bids view with you-vs-team and rule-based tips`

---

### Task 3: `/stats/bids` route (backend)

**Files:** Create `app/routers/stats.py`; Modify `app/main.py` (include router). Test: `tests_unit/test_stats_router.py`.

**Interfaces:** `GET /stats/bids?period&date&to&region&user` → `bids_view(...)`; reuse `metrics._stats_days` for date parsing and `dashboard._subject_id(user, userId)` for the admin rule (import, don't copy); `today` = `datetime.now(app_tz()).date()`.

- [ ] **Step 1: Failing tests** — `test_staff_sees_self`; `test_staff_other_user_403`; `test_admin_views_other_user`; `test_bad_custom_range_400`.
- [ ] **Step 2–4:** Run (FAIL) → implement → run file + full backend suite (only the 2 known failures).
- [ ] **Step 5: Commit** `feat: GET /stats/bids`

---

### Task 4: Frame components + API types (app)

**Files:** Create `src/components/stats/StatsFrame.tsx`, `src/components/stats/RangeBar.tsx`, `src/components/stats/StatsFrame.test.tsx`; Modify `src/api/endpoints.ts` (types `StatsView`, `StatsHeadline`, `StatsTip`, `StatsEveryoneRow`, `StatsTrend`; `getStatsView(tab: 'bids', params: StatsParams & { user?: string })`).

Branch first: `git checkout main && git -c credential.helper= -c 'credential.helper=!gh auth git-credential' pull --ff-only spark main && git checkout -b feat/stats-insights`.

**Interfaces:** `StatsFrame({ view, canPick, onPick }: { view: StatsView; canPick: boolean; onPick: (userId: string) => void })` renders, in order: header grid (each headline: label, value formatted by `format` — `count` → `toLocaleString`, `perDay` → 0 decimals; rank `N of M` or `would rank N of M` when the subject isn't in the team; `▲/▼ x% vs median` colored good/bad by `higherIsBetter`; note) + `RangeBar` for the first headline (min–max track, p25–p75 shaded, median tick labelled "median", subject dot labelled "you"); "What to work on" (tips with severity dot `bad` red / `watch` amber / `good` green, `text`, muted `detail`; when `notEnoughData`: "Not enough data yet — check back after a few working days."; when no tips: "Nothing stands out — you're in line with the team."); "Everyone" horizontal ranked bars (width ∝ value/max, subject highlighted, `vsMedian` %, names clickable only when `canPick`); "Trend" Recharts `LineChart` (you solid, median dashed); `<details>` "How these are counted" listing `definitions`.

- [ ] **Step 1: Failing tests** — `renders header with rank and vs median`; `range bar places you and median`; `not enough data state`; `no tips state`; `everyone highlights subject and only admins can click`; `definitions collapsed by default`.
- [ ] **Step 2–4:** Run (FAIL) → implement → `npx tsc --noEmit && npx vitest run src/components/stats` PASS.
- [ ] **Step 5: Commit** `feat: shared statistics frame (you vs team, tips, everyone, trend)`

---

### Task 5: New Bids tab + Viewing picker (app)

**Files:** Modify `src/components/statistics/statsFilters.tsx` (add `user` URL key, default `''`, kept across tabs, cleared by Clear only for admins' picker), `src/components/statistics/BidsTab.tsx` (replace body with `StatsFrame` fed by `getStatsView('bids', …)` via SWR key `['stats', 'bids', period, date, to, region, user]`, `keepPreviousData`), `src/pages/Statistics.tsx` (tab order: Bids, Interviews, Callers, Resume compare, Earnings, AI usage — Profiles arrives in slice 3). Tests: `src/components/statistics/BidsTab.test.tsx` (new), update `statsFilters.test.tsx`.

**Interfaces:** `StatsFilterBar` gains an admin-only "Viewing" `Select` (options: "Me" = `''` + `useTeamLookup(true)` users) bound to `user`; staff never see it. Clicking a name in Everyone (admins) sets `user`.

- [ ] **Step 1: Failing tests** — `bids tab renders the frame from /stats/bids`; `admin sees Viewing picker and picking sets user in the URL`; `staff has no Viewing picker`; `clicking a name in Everyone (admin) switches the viewed person`; `loading and error states`.
- [ ] **Step 2–4:** Run (FAIL) → implement → `npx tsc --noEmit && npx vitest run` (full) PASS.
- [ ] **Step 5: Commit** `feat: Bids tab shows you vs team with tips; admins can view a teammate`
