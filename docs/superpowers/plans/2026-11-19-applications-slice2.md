# Applications Insights — Slice 2 (Profile report card + coverage line) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Below the you-vs-team header on Statistics → Bids and → Interviews, show a coverage line and a "Your profiles" report card (per profile: bids self / bidder, matured, interviews, per-100 vs team, first / median / slowest days, how far, still in play, grade) built from the `applications` table.

**Architecture:** A pure module `app/stats/profile_report.py` turns application rows into per-profile summaries, a team baseline and grades. A new endpoint `GET /stats/applications/profiles` (same period / region / admin `user` rules as `/stats/{tab}`) loads rows for the period cohort and returns the report. The app fetches it in a new `ProfilesCard` rendered by `FramedTab` under `StatsFrame` on the bids and interviews tabs.

**Tech Stack:** FastAPI + Beanie/Motor (mongomock tests); React + SWR + Tailwind (vitest + Testing Library).

**Spec:** `docs/superpowers/specs/2026-11-19-applications-insights-design.md` (sections "Decided by the user", "The response window", "Page" items 1–2, "Grades", "Decisions from the builder check" 1–3). Slice 1 (table, window) is on the same branch.

**Repos / branches:** backend `/home/eningeer/work/Engineer/engineers_backend` and app `/home/eningeer/work/Engineer/engineers_app`, both on `feat/stats-insights`.

## Global Constraints

- Period = cohort: rows whose `appliedAt` falls in the selected period (app-timezone days `[start, until]` from `app.routers.metrics._stats_days`, converted to naive-UTC bounds `[start 00:00 app tz, until+1 00:00 app tz)`).
- Rows with `bidStatus == "rejected"` are excluded from every count on the card and the coverage line.
- The matured cutoff uses the team window for every profile; the spec's per-profile window (≥ 5 matched) is left for a later slice.
- Matured = `appliedAt <= now_utc - timedelta(days=window["matured_days"])` (window from `app.applications.window.response_window(today)`). Per-100 divides interviews among matured rows by matured rows (× 100, one decimal). Rows younger than that are "still in play" when they have no interview yet.
- Grades (user decision 2026-11-18, replaces the fixed thresholds; constants in `profile_report.py`): `MIN_MATURED = 30`, `EXPECTED_MIN = 3`, `SLOW_RATIO = 2`, `STUCK_INTERVIEWS = 3`. expected = matured × team per-100 / 100. Order: early → dead → strong → watch → early (too few to tell).
  - `early`: matured < 30, or the team per-100 is None.
  - `dead`: expected ≥ 3 and 0 interviews among matured.
  - `strong`: per-100 ≥ team per-100 and at least one interview reached tech or later.
  - `watch`: any of: below team (only when expected ≥ 3); slow (team median days ≥ 1 and median ≥ 2 × team); stuck (≥ 3 interviews, none reached tech); at/above team with interviews but none reached tech.
  - otherwise `early` ("too few to tell"). The "no interview for 21+ days while still bidding" rule is removed.
- Reach buckets from `furthest`: `intro` = interviews; `tech` = furthest in {tech, hiring, panel, final, offer}; `final` = furthest in {final, offer}.
- Team baseline = all rows in the period cohort and region (every owner): team per-100 = median of profile per-100 over profiles with matured ≥ 30 (None when no profile qualifies); team median days = `statistics.median_low` of `daysToInterview` over cohort rows with an interview (None when none).
- Days per profile: first = min, median = `statistics.median_low`, slowest = max of `daysToInterview` over cohort rows with an interview; None when there are none.
- Coverage line copy (Bids and Interviews tabs): `Job-level numbers cover {rows} of {headerBids} bids ({pct}%); typed self bids aren't tied to jobs.` plus, when `unmatchedInterviews > 0`: ` {n} interview(s) couldn't be tied to a job.` `pct` = round(100 × rows / headerBids), omitted (`—`) when headerBids is 0.
- Profile names are the subject's own profiles (`app.plan_profiles.owner_profiles(uid)`, archived included); rows without an `accountId` never appear as a profile.
- Never run tests or scripts against the real DB (backend `.env` is production). Commits single line, no co-author / "Generated with" trailers.
- Backend tests: `.venv/bin/python -m pytest -q -p no:cacheprovider tests_unit/<file>`; full suite with `--ignore=tests_unit/test_resume_experience_roles.py --ignore=tests_unit/test_style_serializer.py --ignore=tests_unit/test_template_annotate_removals.py` (2 known failures: test_bidder_ext upload_url_without_storage, test_resume_prompts bad_lists[prompts5-new-1]). App tests: `npx vitest run <file>`; full `npx vitest run`, `npx tsc --noEmit`.

## Review Focus

1. A profile with matured bids but the whole period still inside the response window (e.g. "This week") → every profile `early`, still-in-play counts shown, no ⛔/⚠ judgement (Task 1 `test_young_cohort_is_early_not_judged`).
2. Interview on a rejected bid, or rows without `accountId` → never counted on a profile; rejected rows also not in coverage `rows` (Task 1 `test_rejected_rows_ignored`, Task 2 `test_coverage_excludes_rejected_and_counts_no_link`).
3. Region filter → both the subject's profiles and the team baseline use only rows of that region (Task 2 `test_region_scopes_profiles_and_team`).
4. Non-admin passing `user=<someone else>` → 403 like `/stats/{tab}`; admin sees that user's profiles (Task 2 `test_user_param_rules`).
5. Subject with no rows (new user, or table empty before the sync ran) → `profiles: []`, coverage rows 0, the card shows an empty state instead of an empty table (Task 2 `test_no_rows_empty_report`, Task 3 `shows the empty state`).

---

### Task 1: Pure per-profile summaries, team baseline and grades

**Files:** Create `app/stats/profile_report.py`. Test `tests_unit/test_profile_report.py`.

**Interfaces (produces):**
- `summarize(rows: list[dict], matured_before: datetime) -> dict[str, dict]` — keyed by `str(accountId)`; rows are application dicts (`accountId`, `appliedAt`, `by`, `bidStatus`, `interviewId`, `daysToInterview`, `furthest`); rejected and accountId-less rows skipped. Value: `{bidsSelf, bidsBidder, matured, maturedInterviews, interviews, per100: float|None, days: {first, median, slowest}|None, reach: {intro, tech, final}, stillInPlay}`.
- `team_baseline(summaries: dict[str, dict], rows: list[dict]) -> dict` → `{per100: float|None, medianDays: int|None, profiles: int}` (`profiles` = qualifying profiles count).
- `grade(summary: dict, team: dict, activity: dict|None, matured_days: int, now: datetime) -> tuple[str, list[str]]` — `activity` = `{firstAt, lastAt, lastRoundAt}` for that profile over all its rows (any period), or None. Returns `(grade, reasons)`; reasons are short English strings, e.g. `"below team (1.2 vs 2.6 per 100)"`, `"slow (median 14 days vs team 6)"`, `"intros don't reach tech"`, `"150+ matured bids, no interview"`, `"no interview for 21+ days"`, `"too early to judge"`.

- [ ] **Step 1: Failing tests** — `test_summarize_counts_self_bidder_matured_and_in_play`; `test_rejected_rows_ignored`; `test_rows_without_account_skipped`; `test_per100_one_decimal_and_none_without_matured`; `test_days_first_median_slowest` (days 2, 5, 13 → first 2, median 5, slowest 13; even count uses median_low); `test_reach_buckets` (furthest intro / tech / hiring / final / offer → intro 5, tech 4, final 2); `test_team_baseline_median_over_profiles_with_30_matured`; `test_team_baseline_none_when_no_profile_qualifies`; grades: `test_grade_dead_150_matured_no_interview`, `test_grade_dead_silent_while_bidding`, `test_grade_not_dead_when_new_profile` (first row younger than matured_days + 21), `test_young_cohort_is_early_not_judged`, `test_grade_strong_needs_tech`, `test_grade_watch_reasons` (below team; slow 2×; stuck ≥ 3 intros no tech).
- [ ] **Step 2: Run** — FAIL (module missing).
- [ ] **Step 3: Implement** per the Global Constraints values.
- [ ] **Step 4: Run** — PASS; full suite.
- [ ] **Step 5: Commit** `feat: profile report — per-profile application summaries, team baseline and grades`

---

### Task 2: `GET /stats/applications/profiles`

**Files:** Modify `app/routers/stats.py` (new route, registered before nothing else changes; two segments so `/stats/{tab}` is untouched). Create `app/stats/profile_loader.py` (DB reads). Test `tests_unit/test_stats_profiles_endpoint.py`.

**Interfaces:**
- Consumes: Task 1 functions; `response_window(today)`; `owner_profiles(uid)`; `interview_rates.counts(...)` and `interview_rates._profile_regions()` exactly as `app/stats/bids.py` uses them for the header's bid total (`[0]` of the subject's tuple); `_stats_days`, `_subject_id` (same subject rule as `stats_tab`); `interview_rates.label(period, start, end)`.
- Produces (response): `{label, subject: {userId, name}, window: {median, p90, matured_days, n, thin}, coverage: {rows, headerBids, noLink, unmatchedInterviews}, team: {per100, medianDays, profiles}, profiles: [{accountId, name, region, archived, ...Task 1 summary fields, grade, reasons}]}`. Profiles sorted by grade order strong, watch, dead, early, then per100 desc (None last), then name.
- `profile_loader.load_period_rows(start: datetime, end: datetime, region: str) -> list[dict]` — one projected query on `applications` (`appliedAt` in `[start, end)`, `region` filter unless "all", fields: key, accountId, ownerId, appliedAt, by, bidStatus, interviewId, daysToInterview, furthest, firstRoundAt). `profile_activity(account_ids: list) -> dict[str, dict]` — one aggregation grouped by accountId: min/max `appliedAt`, max `firstRoundAt`. `subject_unmatched_interviews(owner_id, start, end) -> int` — interviews `createdBy == owner_id`, `createdAt` in the period, countable per `rows.interview_outcome`, `_id` not in the table's interviewIds (same rule as the coverage endpoint; reuse by extracting a shared helper from `applications_coverage` rather than duplicating).
- Coverage: `rows` = subject's non-rejected cohort rows (`ownerId == subject`, includes `src:` rows), `noLink` = those whose key starts with `src:`, `headerBids` = header bid total for the subject, same region.

- [ ] **Step 1: Failing tests** (mongomock + dependency overrides like `tests_unit/test_stats_router.py`) — `test_profiles_report_shape` (two profiles, team rows from another owner; asserts sorting, grades, team per100); `test_coverage_excludes_rejected_and_counts_no_link`; `test_region_scopes_profiles_and_team`; `test_user_param_rules` (non-admin other user → 403; admin → that user's profiles); `test_no_rows_empty_report` (`profiles == []`, coverage rows 0); `test_route_does_not_shadow_stats_tab` (`/stats/bids` still answers). Patch `interview_rates.counts` / `_profile_regions` in tests to fixed numbers (they read other collections).
- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** — PASS; full suite.
- [ ] **Step 5: Commit** `feat: profile report endpoint with coverage for the Bids and Interviews tabs`

---

### Task 3: App — `ProfilesCard` under the frame on Bids / Interviews

**Files:** Modify `src/api/endpoints.ts` (types + `getProfileReport`); Create `src/components/stats/ProfilesCard.tsx`, `src/components/stats/ProfilesCard.test.tsx`; Modify `src/components/stats/FramedTab.tsx` (render `<ProfilesCard params user />` below `<StatsFrame>` when `tab !== 'ai-usage'`), `src/components/stats/FramedTab.test.tsx` if it asserts the full render.

**Interfaces:**
- Consumes: Task 2 response.
- `getProfileReport(params: { period; date; to?; region; user? }) => apiFetch<ProfileReport>('/stats/applications/profiles' + qs(params))`; types `ProfileReport`, `ProfileRow` mirror the response (grade: `'strong' | 'watch' | 'dead' | 'early'`).
- `ProfilesCard({ params, user }: { params: StatsParams; user: string })` — own SWR key `['stats-profiles', period, date, to, region, user]`, `keepPreviousData`.

Layout (panel like `StatsFrame`'s sections):
1. Coverage line (copy in Global Constraints), muted small text.
2. Heading "Your profiles" (admin viewing someone: "{name}'s profiles") and sub-line: `Bids made in {label} that had {matured_days}+ days to work ("matured"), what became of them, and how fast.` + when `window.thin`: ` Not enough interviews yet to measure the team's response time, so 14 days is used.`
3. Table columns: Profile (name + region chip; "archived" muted), Grade (✅ Strong / ⚠ Watch / ⛔ Dead / ⏳ Too early — reasons in `title` and a muted line under the badge on small screens), Bids self / bidder, Matured, Interviews, Per 100 bids (number + small bar scaled to the max of profiles and team, with a team-median tick; `—` when None), Days first · median · slowest (`—` when None), How far (`{intro} intro · {tech} tech · {final} final`, or "no interviews"), Still in play.
4. Legend: `Bar = profile · line = team median ({team.per100} per 100). Grade: ✅ at/above team and reaches tech · ⚠ below team, slow, or stuck at intro · ⛔ 150+ matured bids and no interview, or none for 21 days while still bidding · ⏳ fewer than 30 matured bids.`
5. Empty state (`profiles.length === 0`): `No job-level bids in this period yet.`; loading spinner and error text like FramedTab's. Horizontal scroll on narrow screens (`overflow-x-auto`), no page-level horizontal scroll.

- [ ] **Step 1: Failing tests** — `renders coverage line with percent and unmatched interviews`; `renders a row per profile with grade, per-100 and days`; `shows dashes for missing per-100 and days`; `shows the thin-window note`; `shows the empty state`; `FramedTab renders ProfilesCard on bids and interviews but not ai-usage` (mock `api.getProfileReport`).
- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** — PASS; `npx vitest run`, `npx tsc --noEmit`.
- [ ] **Step 5: Commit** `feat: Statistics profile report card with coverage line on Bids and Interviews`
