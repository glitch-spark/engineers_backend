# Profile Metrics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Per-profile bids/week (self + bidder), interview rounds/week (self + caller), interviews per bid and average
tailor / job-match scores, shown on the Profiles list and each profile page; self bids typed per profile in the
Daily Plan.

**Architecture:** Daily Plan Done stores `bidsSelfByProfile` and derives the region fields from profile regions on
save (`app/plan_profiles.py`). A new `app/profile_metrics.py` computes metrics on request from daily plans, Bid Track
bids, interviews and the applied log; two endpoints on `/accounts` serve it. The SPA adds metric columns to the
Profiles list, a metrics panel on the profile page, and per-profile Done inputs in the Daily Plan.

**Tech Stack:** FastAPI + Beanie (Motor), pytest + mongomock-motor; Vite + React 18 + SWR + Tailwind + Recharts.

**Spec:** `docs/superpowers/specs/2026-11-03-profile-metrics-design.md`

**Repos / branches:** `engineers_backend` and `engineers_app`, both on `feat/new-feature-2`. Tasks 1–4 are backend,
5–7 frontend, 8 is verification. Backend tests: `python -m pytest tests_unit/<file> -v` from `engineers_backend`
(unit tests live in `tests_unit/`; `pytest.ini`'s testpaths is the integration suite, so always pass the path).

## Global Constraints

- Weeks are Monday–Sunday on app-local days (`app.dashboard_stats.app_tz`); only completed weeks count.
- `weeks` ∈ {4, 8, 12}, default 4; anything else → 422.
- Span start = latest of: window's first Monday; first Monday on or after `PROFILE_METRICS_SINCE`; Monday of the week
  holding the profile's `createdAt` (local day).
- Region map: `US` → `bidsSelfUs`, `EU` → `bidsSelfEu`, `Latam` → `bidsSelfLatam`; `Canada`, `Asia`, none → self total
  only. `bidsSelf` = sum of all `bidsSelfByProfile` values.
- Bidder bids: `status` in {`approved`, `pending`}; bidders with `profileId` = profile, archived included.
- Interviews = rounds held, self/caller from `app.interview_rounds.split_rounds_by_day`.
- Low volume: bids < 30 (`app.interview_rates.MIN_BIDS`).
- Rounding: per-week values 1 decimal; `interviewsPerBid` 4 decimals; score `avg` 1 decimal.
- No metric divides by zero: `null` instead; UI shows `—`.
- Visibility: non-admin = own profiles only; admin = `userId`'s profiles, or every active profile without `userId`
  (mirrors `GET /accounts`). Plans: owner writes only.
- Commit messages end with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. A profile is deleted after days were typed for it → metrics ignore the key; re-saving that day from the form
   drops it (the form only sends listed profiles) — test in Task 1 (`test_put_rejects_foreign_profile`) and Task 3
   (`test_self_bids_ignores_unknown_keys`).
2. A profile's region changes after days were saved → stored region fields keep the region at save time; per-profile
   metrics are unaffected — test in Task 1 (`test_region_fields_fixed_at_save`).
3. Bid at the 18:30 Chicago daily cutoff, or a round late in the local evening → counted in the right day/week —
   tests in Task 2 (`test_bid_report_day_cutoff`) and Task 3 (`test_interview_week_uses_local_day`).
4. Admin opens another user's day → `profiles` are the owner's, not the admin's — test in Task 1
   (`test_day_profiles_are_owners`).
5. A profile with interviews but no bids → `interviewsPerWeek` shown, `interviewsPerBid` null, `lowVolume` true —
   test in Task 3 (`test_summarize_zero_bids`).

---

### Task 1: Daily Plan stores self bids per profile

**Files:**
- Modify: `app/models/plan_common.py` (new `DoneCounts`, `BidsIn` field, `_SelfTotal` skip)
- Modify: `app/models/daily_plan.py` (`done: DoneCounts`)
- Create: `app/plan_profiles.py`
- Modify: `app/plan_auto.py` (`effective_daily_done` returns `DoneCounts`)
- Modify: `app/routers/daily_plans.py` (`put_day`, `_day_out`, `get_day`, `list_week`)
- Test: `tests_unit/test_daily_plan_by_profile.py`

**Interfaces:**
- Produces:
  - `plan_common.DoneCounts(Counts)` with `bidsSelfByProfile: Dict[str, Count] = {}`; `BidsIn` gains the same field.
  - `plan_profiles.REGION_FIELD: dict[str, str]` (the region map).
  - `plan_profiles.derive_self_bids(by_profile: dict[str, int], regions: dict[str, Optional[str]]) -> dict[str, int]`
    → `{"bidsSelf", "bidsSelfUs", "bidsSelfEu", "bidsSelfLatam"}`.
  - `plan_profiles.owner_profiles(owner_id) -> list[dict]` → `[{"id", "name", "region", "archived"}]`, every profile
    `createdBy` = owner (archived included), sorted by name.
  - `plan_profiles.profile_regions(owner_id, ids: Iterable[str]) -> dict[str, Optional[str]]`; raises
    `HTTPException(422, detail=f"Not your profile: {id}")` for an id that isn't one of the owner's profiles.
  - Day output (`_day_out`): `done.bidsSelfByProfile` and `profiles` (owner's). Week output: top-level `profiles` once.

- [ ] **Step 1: Write the failing tests** in `tests_unit/test_daily_plan_by_profile.py`, set up like
  `tests_unit/test_interview_rates.py` (mongomock `db` fixture with `UserExtra, Account, DailyPlan, WeeklyPlan,
  Interview, InterviewQuestion`; call `app.routers.daily_plans.put_day` / `get_day` directly). Tests:
  - `test_derive_self_bids`: `derive_self_bids({"a": 5, "b": 3, "c": 2, "d": 4}, {"a": "US", "b": "EU", "c": "Latam", "d": "Canada"})`
    == `{"bidsSelf": 14, "bidsSelfUs": 5, "bidsSelfEu": 3, "bidsSelfLatam": 2}`; a `None` region counts in `bidsSelf` only.
  - `test_put_by_profile_derives_regions`: put `{"done": {"bidsSelfByProfile": {us: 5, asia: 4}, "bidsBidder": 2}}` →
    stored `done.bidsSelfByProfile == {us: 5, asia: 4}`, `bidsSelf == 9`, `bidsSelfUs == 5`, `bidsBidder == 2`.
  - `test_put_rejects_foreign_profile`: another user's profile id → `HTTPException` 422.
  - `test_region_fields_fixed_at_save`: save, change the profile's region to `EU`, read the day → `bidsSelfUs` still 5.
  - `test_put_region_only_unchanged`: put `{"done": {"bidsSelfUs": 3, "bidsSelfEu": 2}}` → `bidsSelf == 5`,
    `bidsSelfByProfile == {}`.
  - `test_day_profiles_are_owners`: admin `get_day(day, admin, userId=str(owner.id))` → `profiles` ids == owner's.
  - `test_week_includes_profiles`: `list_week` response has `profiles` and each day's `done.bidsSelfByProfile`.

- [ ] **Step 2: Run to verify they fail**
  Run: `python -m pytest tests_unit/test_daily_plan_by_profile.py -v` — Expected: FAIL (`ImportError: plan_profiles`).

- [ ] **Step 3: Implement.**
  - `_SelfTotal.self_total_from_regions`: return unchanged when `getattr(self, "bidsSelfByProfile", None)` is truthy.
  - `put_day`: when `body.done.bidsSelfByProfile`, `regions = await profile_regions(user.id, keys)`, then
    `plan.done = DoneCounts(**{**bids_of(body.done), **derive_self_bids(by, regions)}, bidsSelfByProfile=by)`;
    otherwise as today (wrapped in `DoneCounts`). Drop zero entries from `by` before storing.
  - `effective_daily_done`: carry `bidsSelfByProfile` from `plan.done`.
  - `_day_out` gains a `profiles` argument; `get_day`/`put_day` pass `await owner_profiles(uid)`; `list_week` returns
    `profiles` once at the top level and passes `None` to `_day_out` (omit the key there).

- [ ] **Step 4: Run to verify they pass**, then the plan suites for regressions:
  `python -m pytest tests_unit/test_daily_plan_by_profile.py tests_unit/test_interview_rates.py tests_unit/test_dashboard_stats.py -v`
  — Expected: all PASS.

- [ ] **Step 5: Commit** — `feat(daily-plans): type self bids per profile; derive region totals`

---

### Task 2: Metrics window and bid report day

**Files:**
- Modify: `app/config.py` (`PROFILE_METRICS_SINCE: Optional[str] = None`, validated with `_YMD` like other dates);
  `.env.example` (`PROFILE_METRICS_SINCE=2026-11-10` with a one-line comment)
- Create: `app/profile_metrics.py`
- Test: `tests_unit/test_profile_metrics.py`

**Interfaces:**
- Produces in `app/profile_metrics.py`:
  - `WEEK_CHOICES = (4, 8, 12)`
  - `since_date() -> date`: `PROFILE_METRICS_SINCE` parsed, or `date.min` when unset.
  - `span(weeks: int, today: date, since: date, created: date) -> tuple[date, date, int]` → `(first Monday, last
    completed Sunday, n_weeks)`; `n_weeks` = 0 when the start is after the end.
  - `bid_report_day(first_at: datetime, sched: BidSchedule) -> date`: naive-UTC `firstAt` → the local date of the
    daily window (`app.bids.windows`) it falls in, i.e. the date of the first daily cutoff strictly after it.

- [ ] **Step 1: Write the failing tests** (module docstring + mongomock not needed yet):
  - `test_span_full_window`: `span(4, date(2026,11,4), date(2026,1,1), date(2025,1,1)) == (date(2026,10,5), date(2026,11,1), 4)`
  - `test_span_since_midweek`: since `2026-10-21` → `(date(2026,10,26), date(2026,11,1), 1)`
  - `test_span_since_monday`: since `2026-10-19` → `(date(2026,10,19), date(2026,11,1), 2)`
  - `test_span_created_midweek`: created `2026-10-28` → `(date(2026,10,26), date(2026,11,1), 1)`
  - `test_span_no_completed_week`: since `2026-11-03` → `n_weeks == 0`
  - `test_bid_report_day_cutoff` (schedule `America/Chicago`, daily `18:30`): `datetime(2026,10,20,23,29)` → `date(2026,10,20)`;
    `datetime(2026,10,20,23,30)` → `date(2026,10,21)`.

- [ ] **Step 2: Run** `python -m pytest tests_unit/test_profile_metrics.py -v` — Expected: FAIL (module missing).

- [ ] **Step 3: Implement** the four names above. `bid_report_day`: convert to `sched.tz`; the day is the local date,
  plus one day when the local time is at or after `sched.daily_at`.

- [ ] **Step 4: Run** the same command — Expected: PASS.

- [ ] **Step 5: Commit** — `feat(profile-metrics): metrics window and bid report day`

---

### Task 3: Metric loaders and summary

**Files:**
- Modify: `app/profile_metrics.py`
- Test: `tests_unit/test_profile_metrics.py` (add a mongomock `db` fixture with `UserExtra, Account, DailyPlan,
  Interview, InterviewQuestion, Bid, Bidder, JobApplicationLog, ResumeApplication`)

**Interfaces:**
- Consumes: `span`, `bid_report_day`, `since_date` (Task 2); `DoneCounts.bidsSelfByProfile` (Task 1).
- Produces (all keys are account id hex strings; `monday` is the week's Monday as `date`):
  - `self_bids(owner_ids: list, account_ids: set[str], start: date, end: date) -> dict[tuple[str, date], int]` —
    logged days only.
  - `bidder_bids(account_ids: list[PydanticObjectId], start: date, end: date, sched: BidSchedule) -> dict[tuple[str, date], int]`
  - `interview_rounds(account_ids: list[PydanticObjectId], start: date, end: date) -> dict[tuple[str, date], list[int]]`
    → `[self, caller]`. Query `Interview` by `accountId` with the same `$or` on `scheduledAt` /
    `stageHistory.scheduledAt` as `load_interviews_for_window`, then `split_rounds_by_day(iv, first, last, app_tz())`.
  - `scores(account_ids: list[str], start: date, end: date) -> dict[str, dict[str, list[int]]]` → `{"tailored": [...],
    "match": [...]}`; `appliedAt` in `[local_day_start_utc(start), local_day_start_utc(end + 1 day))`.
  - `summarize(account_id: str, start: date, n_weeks: int, weekly: dict, score_lists: dict) -> dict` → one row in the
    spec's JSON shape plus `spanWeeks`; `weekly` maps `monday -> {"selfBids", "bidderBids", "selfInterviews",
    "callerInterviews"}` already clipped to the profile's span.
  - `peer_average(rows: list[dict]) -> Optional[dict]` — mean of each numeric field over rows where it isn't null
    (`tailorScore`/`matchScore` → `{"avg": mean of avgs, "n": sum of n}`); `None` for an empty list.
  - `async collect(accounts: list[Account], weeks: int, today: date) -> dict[str, tuple[date, int, dict, dict]]` —
    `accountId -> (start, n_weeks, weekly, score_lists)`, loading every source once over the widest span.

- [ ] **Step 1: Write the failing tests:**
  - `test_self_bids_logged_days_only`: two plans, one with `loggedAt=None` → only the logged one counts.
  - `test_self_bids_ignores_unknown_keys`: a key not in `account_ids` is absent from the result.
  - `test_bidder_bids_statuses`: approved + pending counted, rejected not; an archived bidder's bids counted.
  - `test_interview_rounds_self_caller`: one self round, one caller round (`caller.enabled`) → `[1, 1]`; a canceled
    round not counted.
  - `test_interview_week_uses_local_day` (`APP_TIMEZONE=America/New_York`): a round at `datetime(2026,10,26,3,0)` UTC
    (Sun Oct 25 23:00 local) lands in week `date(2026,10,19)`, not `2026-10-26`.
  - `test_scores_split`: log entry with `resumeId` + `score.total=70` → match; `tailoredJobId` → ResumeApplication
    with `matchScore.total=80` and no `fixedFrom` → tailored; one with `fixedFrom` and `matchScore.total=60` → match;
    entry without a score skipped → `{"tailored": [80], "match": [70, 60]}`.
  - `test_summarize_values`: 4 weeks, self 120, bidder 48, self rounds 8, caller 2, scores `[80, 82]` / `[70]` →
    `bidsPerWeek 42.0`, `selfBidsPerWeek 30.0`, `bidderBidsPerWeek 12.0`, `interviewsPerWeek 2.5`,
    `interviewsPerBid 0.0595`, `lowVolume False`, `tailorScore {"avg": 81.0, "n": 2}`, `matchScore {"avg": 70.0, "n": 1}`.
  - `test_summarize_zero_bids`: 0 bids, 2 rounds over 2 weeks → `interviewsPerWeek 1.0`, `interviewsPerBid None`,
    `lowVolume True`.
  - `test_summarize_zero_weeks`: `n_weeks=0` → every per-week value and `interviewsPerBid` are `None`.
  - `test_peer_average`: two rows → field means; `peer_average([]) is None`.

- [ ] **Step 2: Run** `python -m pytest tests_unit/test_profile_metrics.py -v` — Expected: new tests FAIL.

- [ ] **Step 3: Implement** the names above. `collect` uses each account's own `span(...)` with
  `since_date()` and its `createdAt` local date, loads over the earliest start, and keeps only Mondays ≥ that
  account's start.

- [ ] **Step 4: Run** the same command — Expected: PASS.

- [ ] **Step 5: Commit** — `feat(profile-metrics): bids, interview rounds and scores per profile`

---

### Task 4: Metrics endpoints

**Files:**
- Modify: `app/routers/accounts.py` (two routes declared **before** `@router.get("/{account_id}")`)
- Test: `tests_unit/test_profile_metrics_router.py`

**Interfaces:**
- Consumes: `collect`, `summarize`, `peer_average`, `WEEK_CHOICES` (Task 3).
- Produces:
  - `GET /accounts/metrics?weeks=4&userId=` → `list_profile_metrics(user, weeks: int = 4, userId: str = "") -> dict`
    `{"start", "end", "weeks", "rows": [...]}` over active profiles in scope (see Global Constraints). `start`/`end`
    = the window without the per-profile clamp.
  - `GET /accounts/{account_id}/metrics?weeks=4` → `get_profile_metrics(account_id, user, weeks: int = 4) -> dict`:
    the row plus `"weekly": [{"week", "selfBids", "bidderBids", "selfInterviews", "callerInterviews"}]` (every week
    of the span, zeros included, oldest first) and `"peers"` (= `peer_average` of the owner's other active profiles).

- [ ] **Step 1: Write the failing tests** (call the route functions directly):
  - `test_list_shape`: user with two profiles → two rows with every key from the spec's JSON.
  - `test_list_weeks_validated`: `weeks=5` → `HTTPException` 422.
  - `test_list_other_user_forbidden`: non-admin with another `userId` → 403.
  - `test_list_admin_user_filter`: admin with `userId` → only that user's profiles.
  - `test_detail_not_owner_404`: another user's profile → 404.
  - `test_detail_weekly_and_peers`: 4-week span → 4 `weekly` entries; single-profile owner → `peers is None`.

- [ ] **Step 2: Run** `python -m pytest tests_unit/test_profile_metrics_router.py -v` — Expected: FAIL.

- [ ] **Step 3: Implement** the two routes; access checks reuse the patterns in `list_accounts` / `get_account`.

- [ ] **Step 4: Run** the router and metrics tests, then all of `tests_unit/` —
  `python -m pytest tests_unit -q` — Expected: all PASS.

- [ ] **Step 5: Commit** — `feat(accounts): per-profile metrics endpoints`

---

### Task 5: Daily Plan form with per-profile Done (frontend)

**Files:**
- Modify: `src/api/endpoints.ts` (types), `src/lib/reportWeek.ts`, `src/components/report/GoalDoneFields.tsx`,
  `src/components/report/DayPanel.tsx`, `src/components/report/DayRow.tsx`, `src/components/report/WeekPanel.tsx`
  (pass `profiles` through where `GoalDoneFields` shows Done)

**Interfaces:**
- Consumes: Task 1's day/week response (`done.bidsSelfByProfile`, `profiles`).
- Produces:
  - `endpoints.ts`: `PlanProfile { id: string; name: string; region: string | null; archived: boolean }`;
    `Counts.bidsSelfByProfile?: Record<string, number>`; `DayPlan.profiles?: PlanProfile[]`; week response
    `profiles?: PlanProfile[]`.
  - `reportWeek.ts`: `REGION_FIELD: Record<string, SelfRegionField>` (`US`, `EU`, `Latam`) and
    `withProfileBids<T extends Bids>(c: T, byProfile: Record<string, number>, profiles: PlanProfile[]): T` — sets
    `bidsSelfByProfile`, the three region fields and `bidsSelf` exactly as the server's `derive_self_bids`.
  - `GoalDoneFields` new optional prop `profiles?: PlanProfile[]`.

- [ ] **Step 1: Implement.** In `GoalDoneFields`, when `profiles` is given and `showDone`:
  - the three `Self (US/EU/LATAM)` rows keep their Goal cell; their Done cell becomes a read-only `ValueBox`
    (title "Sum of your <region> profiles");
  - a `Self (other regions)` read-only row appears when Canada / Asia / no-region profiles have bids;
  - below, one row per listed profile — active ones, plus archived ones with a value — labelled
    `Self · <name> (<region or "no region">)`, Goal cell `—`, Done `NumberInput` (read-only `ValueBox` when
    `readOnly`), `onChange` → `onDone(withProfileBids(done, next, profiles))`;
  - when the day has region values but no `bidsSelfByProfile` and is editable, show under the profile rows:
    "Saved by region: US n · EU n · LATAM n — typing per profile replaces it".
  Without `profiles` the component renders as today (Week goal editing). `DayPanel` passes `data.profiles` and sends
  `bidsSelfByProfile` in the save body (extend `bidsOf` to include it when present). `DayRow` shows per-profile
  values (name: n) when `bidsSelfByProfile` is non-empty, else today's region display.

- [ ] **Step 2: Verify** `npm run lint` — Expected: exits 0.

- [ ] **Step 3: Manual check** (local backend on this branch + `npm run dev`): log today with two profiles in
  different regions → Self (US) Done shows the US profile's number, the save succeeds, reopening the day
  shows the values; an older region-only day shows the "Saved by region" hint.

- [ ] **Step 4: Commit** (in `engineers_app`) — `feat(report): type self bids per profile in the daily follow-up`

---

### Task 6: Profiles list metric columns (frontend)

**Files:**
- Modify: `src/api/endpoints.ts`, `src/pages/Accounts.tsx`
- Create: `src/lib/profileMetrics.ts` (formatting + period storage shared with Task 7)

**Interfaces:**
- Consumes: `GET /accounts/metrics` (Task 4).
- Produces:
  - `endpoints.ts`: `ScoreStat { avg: number | null; n: number }`; `ProfileMetricsRow` (fields of the spec's JSON +
    `spanWeeks`); `getProfileMetrics(params: { weeks: number; userId?: string })`.
  - `profileMetrics.ts`: `WEEK_OPTIONS = [4, 8, 12] as const`; `readWeeks(): number` / `writeWeeks(n: number)`
    (`localStorage` key `profileMetricsWeeks`, try/catch, default 4); `fmtPerWeek(n: number | null): string` (1 decimal,
    trailing `.0` dropped, `—` for null); `fmtRate(n: number | null): string` (`6.0%`, `—`); `fmtScore(s: ScoreStat | null): string`
    (rounded integer or `—`).

- [ ] **Step 1: Implement.** Toolbar gets a `Period` select (`Last 4 / 8 / 12 weeks`). SWR key
  `['account-metrics', weeks, isAdmin ? userId : '']` → map by `accountId`. Columns after Region: **Bids/wk**,
  **Interviews/wk**, **Intv/bid**, **Score** as in spec §4 (split line in `text-[11px] text-muted`; low-volume and
  score `n` in `title`). Country column removed; its flag goes before the region text in the Region cell. Metric
  headers are buttons that sort the current page's rows (desc first, nulls last; second click asc). Loading → `skeleton`
  cells; error → `—` cells and one `text-xs` line above the table: "Couldn't load metrics". Rows with
  `spanWeeks === 0` show `—` with title "Not enough history yet". Archived-status view: metric cells `—`.

- [ ] **Step 2: Verify** `npm run lint` — Expected: exits 0.

- [ ] **Step 3: Manual check:** Profiles list shows the four columns, sorting reorders rows, switching the period
  refetches, stopping the backend's metrics route (or offline) leaves the list usable with the error line, the page
  has no horizontal page scroll at 375px (table scrolls inside its container as today).

- [ ] **Step 4: Commit** — `feat(profiles): metric columns and period picker on the Profiles list`

---

### Task 7: Profile page metrics panel (frontend)

**Files:**
- Create: `src/components/profiles/ProfileMetricsPanel.tsx`
- Modify: `src/api/endpoints.ts`, `src/pages/AccountEdit.tsx`

**Interfaces:**
- Consumes: `GET /accounts/{id}/metrics` (Task 4); `profileMetrics.ts` helpers (Task 6).
- Produces: `endpoints.ts` `ProfileMetricDetail extends ProfileMetricsRow { weekly: {week: string; selfBids: number;
  bidderBids: number; selfInterviews: number; callerInterviews: number}[]; peers: ProfileMetricsRow | null }`,
  `getProfileMetric(id: string, params: { weeks: number })`; component
  `ProfileMetricsPanel({ accountId }: { accountId: string })`.

- [ ] **Step 1: Implement.** A `panel p-6` section rendered first on an existing profile (not `isNew`): header
  "Metrics" + the same Period select (shared `readWeeks`/`writeWeeks`); four tiles — Bids/wk (self · bidder),
  Interviews/wk (self · caller), Interviews/bid (low-volume note), Avg scores (Tailor n / Job match n). Each tile's
  "vs. your other profiles: X" line with `TrendingUp` / `TrendingDown` (lucide) when `peers` is non-null and both
  values are non-null. Below, a Recharts `ComposedChart` (height 200): stacked `Bar`s `selfBids` / `bidderBids`,
  `Line` `selfInterviews + callerInterviews` on a right axis; colours from the tokens `InterviewRateTab` uses.
  `spanWeeks === 0` → tiles show `—` and "Not enough history yet"; fetch error → one inline message, rest of the page
  unaffected.

- [ ] **Step 2: Verify** `npm run lint` — Expected: exits 0.

- [ ] **Step 3: Manual check:** profile page shows tiles + chart; with one profile the peers line is hidden; at 375px
  tiles stack in one column.

- [ ] **Step 4: Commit** — `feat(profiles): metrics panel on the profile page`

---

### Task 8: Verification and release notes

- [ ] **Step 1:** `engineers_backend`: `python -m pytest tests_unit -q` — Expected: all PASS.
- [ ] **Step 2:** `engineers_app`: `npm run lint && npm run build` — Expected: both exit 0.
- [ ] **Step 3:** End-to-end in the local app with seeded data: type per-profile bids for two days, add a bidder bid
  (pending) and an interview round on one profile, set `PROFILE_METRICS_SINCE` to a Monday ≥ 1 completed week back;
  Profiles list and profile page show the expected numbers for that week.
- [ ] **Step 4:** Note for the PR descriptions: set `PROFILE_METRICS_SINCE` (release Monday) on Railway; frontend and
  backend must ship together (the Daily Plan form needs the new `profiles` field).
