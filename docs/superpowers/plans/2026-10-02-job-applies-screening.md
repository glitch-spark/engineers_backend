# Job Applies Screening Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Check every job in a sheet before profiles are chosen, show a screening report (buckets + location groups with fitting profiles pre-ticked), then score only the job × profile pairs the user confirms.

**Architecture:** The existing run gains a pause. `phase="screen"` runs fetch → extract → job gates and ends at `status="screened"`; `POST /runs/{id}/start` stores per-group assignments and runs `phase="score"`. Bucket, group and fit rules are pure functions in a new `app/job_apply/screening.py`; the report is computed from rows on request. The frontend adds a Layout A report on the run page and slims the New run panel.

**Tech Stack:** FastAPI, Beanie/MongoDB, pytest (asyncio auto mode); React 18 + TypeScript + SWR + Tailwind.

**Spec:** `docs/superpowers/specs/2026-10-02-job-applies-screening-design.md` (engineers_backend)

## Global Constraints

- Backend branch `feat/job-screening` in `engineers_backend` (worktree `engineers_backend-job-applies`), from `dev`. Frontend branch `feat/job-screening` in `engineers_app` (worktree `engineers_app-job-applies`), from `main`.
- Bucket order (first match wins): `closed`, `not_fetched`, `read_failed`, `clearance`, `onsite`, `too_old`, `check`, `valid`. "Worth applying to" = `valid` + `check`.
- `closed` = `fetch_failed` whose `statusReason` starts with `"job closed"` (raised by `JobClosed` in `fetch.py`).
- `forceInclude` allowed only for `clearance`, `onsite`, `too_old`; it yields `check`.
- Group key: normalised allowed locations, de-duplicated, sorted, joined with `+`; empty → `none`.
- Assignment key `*` applies to every group.
- `ACTIVE` stays `("queued", "running")`; a `screened` run never blocks another run.
- Posting cache reuse window: 24 hours (`POSTING_REUSE`). `CACHE_DAYS` (document TTL) unchanged.
- `PATCH maxAgeDays` only while `screened`; range 1–365 like `POST /runs`.
- Run with `selection` at creation keeps today's behaviour: `autoStart=True`, `assignments={"*": [...]}`, no pause.
- Commit messages: `feat(job-applies): …` / `test(job-applies): …`, ending with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Backend unit tests: `../engineers_backend/.venv/Scripts/python.exe -m pytest tests_unit -q --continue-on-collection-errors` (3 collection errors are pre-existing on `dev`: `test_resume_experience_roles`, `test_style_serializer`, `test_template_annotate_removals`). API tests need `MONGODB_URI_TEST` (see `tests/README.md`); if it is not set, say so in the task report rather than skipping silently.
- Frontend check: `npm run lint` (tsc) in `engineers_app-job-applies`.

## Review Focus

1. **Profile edited or archived between screening and start** — the report reads profiles live, and `/start` rejects archived/foreign accounts with 404 like `_parse_selection` does. Test in Task 4.
2. **Double-click on "Score & continue"** — the second `/start` gets 409 ("Run is not waiting for review") and the button is disabled while the request is pending. Tests in Task 4 and Task 7.
3. **Retry from `screened` keeps manual choices** — `forceInclude` survives a retry, and rows that come back fetched land in their real bucket; the run pauses at `screened` again. Test in Task 4.
4. **1,000-row run report stays fast** — `/screening` projects only `screen` and `groupKey` (never `jdText`). Test in Task 4 asserts the projection model has exactly those fields.
5. **Max age changed while the run is not `screened`** (queued by a retry, or already scoring) — 409, rows untouched. Test in Task 4.

---

### Task 1: Model fields and statuses

**Files:**
- Modify: `app/models/job_apply.py`
- Test: `tests_unit/test_job_apply_models.py`

**Interfaces:**
- Produces: `RunStatus` += `"screened"`; `RowStatus` += `"unassigned"`; `ScreenBucket = Literal["valid", "check", "closed", "not_fetched", "read_failed", "clearance", "onsite", "too_old"]`; `RunPhase = Literal["screen", "score"]`.
- `JobApplyRun`: `phase: RunPhase = "screen"`, `assignments: Dict[str, List[PydanticObjectId]] = {}`, `autoStart: bool = False`, `screenedAt: Optional[datetime] = None`; `selection: List[RunSelection] = []` (now defaults to empty).
- `JobApplyRow`: `screen: Optional[ScreenBucket] = None`, `groupKey: Optional[str] = None`, `forceInclude: bool = False`.

- [ ] **Step 1: Write the failing test** `test_screening_defaults`: a `JobApplyRun(userId=…, fileName="x")` (no `selection`) has `phase == "screen"`, `assignments == {}`, `autoStart is False`, `selection == []`; a `JobApplyRow(runId=…, userId=…, rowIndex=1)` has `screen is None`, `groupKey is None`, `forceInclude is False`; `"screened" in get_args(RunStatus)` and `"unassigned" in get_args(RowStatus)`.
- [ ] **Step 2: Run** `pytest tests_unit/test_job_apply_models.py -q` → FAIL (`selection` required / attributes missing).
- [ ] **Step 3: Add the literals and fields** listed under Interfaces.
- [ ] **Step 4: Run** the models tests → PASS; run the full unit suite → no new failures.
- [ ] **Step 5: Commit** `feat(job-applies): run phase, assignments and row screening fields`.

### Task 2: Screening rules (`screening.py`)

**Files:**
- Create: `app/job_apply/screening.py`
- Test: `tests_unit/test_job_apply_screening.py`

**Interfaces:**
- Consumes: `gates.covers`, `gates.normalize_country`, `gates.region_key`, `gates.job_gates`, `gates.blocked`, `worker.failing_reasons` (move `failing_reasons` into `gates.py` if importing `worker` would be circular; keep `worker.failing_reasons` as a re-export), `schema.Location`, `schema.GateResult`, models from Task 1.
- Produces:
  - `WORTH: tuple[str, ...] = ("valid", "check")`
  - `FORCEABLE: tuple[str, ...] = ("clearance", "onsite", "too_old")`
  - `bucket(*, status: str, status_reason: Optional[str], gates: list[GateResult], group_key: Optional[str], force_include: bool) -> Optional[ScreenBucket]` — `None` while the row is `pending`/`fetched`.
  - `group_key(locations: list[Location]) -> str`
  - `profile_fits(key: str, *, country: Optional[str], region: Optional[str]) -> bool`
  - `rescreen(row: JobApplyRow, *, run_date: date, max_age_days: int) -> None` — recomputes `row.gates` (from `row.extraction` and `row.postedDate`), `row.status` (`extracted`/`excluded`, only when it was one of those), `row.statusReason`, `row.screen`.
  - `build_report(rows: list[tuple[Optional[str], Optional[str]]], profiles: list[dict], *, max_age_days: int) -> dict` — `rows` are `(screen, groupKey)`; `profiles` are `{"id", "name", "country", "region", "resumes"}`; returns the spec §4.3 `ScreeningReport` shape, groups only over worthwhile rows, sorted by `jobs` desc then `key`.

- [ ] **Step 1: Write the failing tests**
  - `test_bucket_order`: `fetch_failed` + reason `"job closed (HTTP 410)"` → `closed`; `fetch_failed` + `"no job text on the page"` → `not_fetched`; `llm_failed` → `read_failed`; gates with clearance fail + remote fail → `clearance`; remote fail + freshness fail → `onsite`; freshness fail only → `too_old`; all pass but freshness `unknown` → `check`; all pass and `group_key="none"` → `check`; all pass, group `GB` → `valid`; status `pending` → `None`.
  - `test_force_include`: freshness fail + `force_include=True` → `check`; `fetch_failed` + `force_include=True` → `not_fetched`.
  - `test_group_key`: `[country "United Kingdom", country "GB"]` → `"GB"`; `[country "US", region "EU"]` → `"EU+US"`; `[region "worldwide"]` → `"Worldwide"`; `[region "Europe"]` → `"Europe"`; `[region "emea"]` → `"EMEA"`; `[]` → `"none"`.
  - `test_profile_fits`: `("GB", country="GB")` True; `("GB", country="US")` False; `("Europe", country="DE")` True; `("EU+US", country="US")` True; `("none", country="US")` True; `("US", country=None, region=None)` True; `("Worldwide", country="BR")` True.
  - `test_rescreen_max_age`: a row `excluded` only by freshness (posted 40 days before run date) becomes `extracted`/`valid` with `max_age_days=60`, and back to `excluded`/`too_old` with 30.
  - `test_build_report`: rows `[("valid","GB"),("check","none"),("closed",None),("too_old","US")]`, profiles Sam (GB) and Alex (US) → `total 4, worth 2, check 1`, `buckets["closed"] == 1`, groups `[{"key":"GB","jobs":1,"check":0,"fits":[sam]}, {"key":"none","jobs":1,"check":1,"fits":[alex, sam]}]` (fits ordered as `profiles`), `maxAgeDays` echoed, every bucket key present with 0 defaults.
- [ ] **Step 2: Run** `pytest tests_unit/test_job_apply_screening.py -q` → FAIL (module missing).
- [ ] **Step 3: Implement `screening.py`.** Region labels for `group_key`: `{"worldwide"|"anywhere"|"global": "Worldwide", "americas": "Americas", "north america": "North America", "latam"|"latin america"|"south america": "Latam", "emea": "EMEA", "europe": "Europe", "eu": "EU", "apac": "APAC", "asia": "Asia"}`; try `normalize_country` first (so "US", "United States", "Canada" become ISO-2); unrecognised values are kept stripped. `profile_fits` rebuilds `Location`s from the key parts (two upper-case letters → country, else region) and uses `covers`.
- [ ] **Step 4: Run** the screening tests → PASS; full unit suite → no new failures.
- [ ] **Step 5: Commit** `feat(job-applies): screening buckets, location groups and profile fit`.

### Task 3: Worker phases, assignments and posting reuse

**Files:**
- Modify: `app/job_apply/worker.py`
- Test: `tests_unit/test_job_apply_worker.py` (pure helpers), `tests/test_job_applies.py` (lifecycle)

**Interfaces:**
- Consumes: Task 1 fields, Task 2 `bucket`, `group_key`, `WORTH`.
- Produces:
  - `POSTING_REUSE = timedelta(hours=24)`; `posting_reusable(fetched_at: datetime, now: datetime) -> bool`.
  - `assigned_profiles(assignments: dict[str, list], key: Optional[str]) -> set[str]` — union of `assignments.get(key)` and `assignments.get("*")`, as strings.
  - Rows get `screen`/`groupKey` set on fetch failure, after extraction, and on `llm_failed` (add both names to `_FETCH_FIELDS` and `_EXTRACT_FIELDS`).
  - `run_worker`: screen phase = fetch + extract, sets `screenedAt`; then `autoStart` → `phase="score"` and continue, else `status="screened"` and return. Score phase = `_score_stage` only, then `done`.
  - `_score_stage`: only rows with `screen in WORTH`; resumes/profiles filtered to `assigned_profiles(run.assignments, row.groupKey)`; none → `status="unassigned"` (saved, not scored).
  - `resume_stale_runs` unchanged in effect (it never picks `screened`).

- [ ] **Step 1: Write the failing unit tests** `test_posting_reusable` (fetched 23 h ago → True, 25 h ago → False) and `test_assigned_profiles` (`{"GB": [a], "*": [b]}` with `"GB"` → `{a, b}`; with `"US"` → `{b}`; `{}` → `set()`).
- [ ] **Step 2: Write the failing API tests** in `tests/test_job_applies.py`, with a `create_screen_run(client, headers, data=SHEET)` helper that posts without `selection`:
  - `test_screen_phase_pauses`: after `run_worker`, run `status == "screened"`, `phase == "screen"`, no row has `scoredAt`; the hybrid row has `screen == "onsite"`; the remote rows have `screen` in `("valid", "check")` and `groupKey == "US"`.
  - `test_create_with_selection_runs_through`: the existing `create_run` helper still ends `done` with the same assertions as `test_create_run_and_results` (keep that test unchanged and passing).
  - `test_closed_fetch_is_bucketed`: `FETCH` patched to raise `JobClosed("job closed (HTTP 404)")` → that row `screen == "closed"`.
- [ ] **Step 3: Run** both → FAIL.
- [ ] **Step 4: Implement** the Interfaces above. In `_fetch_one`, reuse the cache only when `posting_reusable(cached.fetchedAt, datetime.utcnow())`.
- [ ] **Step 5: Run** unit + API tests → PASS (API tests only if `MONGODB_URI_TEST` is set).
- [ ] **Step 6: Commit** `feat(job-applies): screen phase pauses for review; score only assigned pairs`.

### Task 4: API endpoints

**Files:**
- Modify: `app/routers/job_applies.py`
- Test: `tests/test_job_applies.py`

**Interfaces:**
- Consumes: Tasks 1–3.
- Produces:
  - `POST /runs`: `selection: str = Form("")`. Empty → `phase="screen"`, `selection=[]`. Non-empty → as today plus `autoStart=True`, `assignments={"*": [ids]}`.
  - `GET /runs/{id}/screening` → `build_report(...)`; rows read via a projection model `_ScreenView(BaseModel)` with exactly `screen: Optional[str]`, `groupKey: Optional[str]`; profiles = non-archived accounts the user can see (`Account.find({"archived": {"$ne": True}})` filtered by `_can_see(acc.createdBy, user)`, the rule `_parse_selection` uses), each with `resumes = len(acc.resumes)`.
  - `GET /runs/{id}/rows`: new `screen: str = Query("")` filter (`{"screen": screen}` added to the query when set).
  - `PATCH /runs/{id}`: `RunPatch` fields both optional: `threshold: Optional[int] = Field(None, ge=0, le=100)`, `maxAgeDays: Optional[int] = Field(None, ge=1, le=365)`. `maxAgeDays` on a run that is not `screened` → 409 "Max age can only change before scoring starts."; otherwise update the run and `rescreen` + save every row with an `extraction`.
  - `PUT /rows/{id}/include` body `{"include": bool}`: 400 "Only jobs excluded for clearance, on-site or age can be included." when the row's bucket (ignoring `forceInclude`) is not in `FORCEABLE`; else set `forceInclude`, recompute `screen`, return the row.
  - `POST /runs/{id}/start` body `{"assignments": {key: [accountId]}, "selection": [{accountId, resumeIds}], "threshold": int}`: 409 "Run is not waiting for review." unless `screened`; 409 when another run is active; account ids validated with `_parse_selection` (404 for accounts the user can't see) plus a new archived check in `_parse_selection` (404 for archived accounts; this also applies to `POST /runs`); 400 "Pick at least one profile for a group with jobs." when no assignment reaches a worthwhile row. Sets `assignments`, `selection`, `threshold`, `phase="score"`, `status="queued"`, starts the worker; returns `_run_out(run)`.
  - `POST /runs/{id}/retry-failed`: from `screened`, reset `fetch_failed`/`llm_failed` rows to `pending` (keeping `forceInclude`), set `status="queued"`, `phase` stays `screen`.

- [ ] **Step 1: Write the failing API tests:**
  - `test_screening_report`: after the screen phase, `/screening` has `total == 3`, `buckets["onsite"] == 1`, one group `"US"` with the user's US account in `fits`.
  - `test_start_scores_assigned_only`: two accounts (US and GB); start with `{"US": [us_acc]}` → rows in group `US` have suggestions only from `us_acc`; run ends `done`.
  - `test_start_twice_409` (Review Focus 2) and `test_start_requires_screened`.
  - `test_start_archived_profile_404` (Review Focus 1): archive the account after screening, then start → 404.
  - `test_max_age_patch_rescreens` and `test_max_age_patch_409_when_not_screened` (Review Focus 5).
  - `test_include_anyway`: on the hybrid row → `screen == "check"`; on a `fetch_failed` row → 400.
  - `test_retry_from_screened_keeps_force_include` (Review Focus 3): a forced row stays `check` after retry; the run is `screened` again after `run_worker`.
  - `test_screen_view_projection` (Review Focus 4): `set(_ScreenView.model_fields) == {"screen", "groupKey"}`.
  - `test_rows_screen_filter`: `?screen=onsite` returns only the hybrid row.
- [ ] **Step 2: Run** `pytest tests/test_job_applies.py -q` → new tests FAIL.
- [ ] **Step 3: Implement** the endpoints above.
- [ ] **Step 4: Run** the API tests and the unit suite → PASS.
- [ ] **Step 5: Commit** `feat(job-applies): screening report, start, include-anyway and max-age endpoints`.

### Task 5: Frontend API types and calls

**Files:**
- Modify: `src/api/endpoints.ts` (job-applies section, ~lines 1513–1860)
- Modify: `src/components/jobApplies/format.ts`

**Interfaces:**
- Produces (endpoints.ts):
  - `JobApplyRunStatus` += `'screened'`; `JobApplyRowStatus` += `'unassigned'`; `JobApplyRun` += `phase: 'screen' | 'score'; assignments: Record<string, string[]>; screenedAt?: string | null`.
  - `JobApplyScreenBucket = 'valid' | 'check' | 'closed' | 'not_fetched' | 'read_failed' | 'clearance' | 'onsite' | 'too_old'`; `JobApplyRow` += `screen?: JobApplyScreenBucket | null; groupKey?: string | null; forceInclude?: boolean`.
  - `JobApplyScreening { total; worth; check; buckets: Record<JobApplyScreenBucket, number>; maxAgeDays; groups: { key: string; jobs: number; check: number; fits: string[] }[]; profiles: { id: string; name: string; country: string | null; region: string | null; resumes: number }[] }`.
  - `createJobApplyRun(source, selection?: …, threshold?, maxAgeDays?)` — omit `selection` from the form when undefined.
  - `getJobApplyScreening(id)`, `startJobApplyRun(id, body: { assignments: Record<string, string[]>; selection: { accountId: string; resumeIds: string[] }[]; threshold: number })`, `setJobApplyRowInclude(rowId, include: boolean)`, `updateJobApplyRun(id, body: { threshold?: number; maxAgeDays?: number })`, `listJobApplyRows(..., screen?: JobApplyScreenBucket)`.
- Produces (format.ts): `locationLabel(key: string): string` (`none` → "Location not stated"; ISO-2 codes via `Intl.DisplayNames(['en'], { type: 'region' })`; `+` → " · "); `BUCKET_LABEL: Record<JobApplyScreenBucket, string>` = Worth applying, Needs a check, Closed, Not fetched, Couldn't read, Clearance required, On-site / hybrid, Too old; `isActive` unchanged.

- [ ] **Step 1: Add types, calls and helpers** as above.
- [ ] **Step 2: Run** `npm run lint` → passes (fix any existing call site of `createJobApplyRun` / `updateJobApplyRun` that the signature change breaks).
- [ ] **Step 3: Commit** `feat(job-applies): screening API types and calls`.

### Task 6: New run panel: sheet + "Check jobs"

**Files:**
- Modify: `src/components/jobApplies/NewRunPanel.tsx`

**Interfaces:**
- Consumes: Task 5 `createJobApplyRun` without selection.
- Produces: the panel keeps Step 1 (sheet source + preview, unchanged) and replaces Steps 2–3 with one action **Check jobs** (disabled until the preview is `ok`), which creates the run with `maxAgeDays` 30 and navigates to `/job-applies/{runId}`. Profile, resume, threshold and filter controls move out (Task 7 reuses the resume-picker markup; extract it to `ProfilePicker.tsx` if it is more than ~40 lines).

- [ ] **Step 1: Implement** the slimmed panel; keep the 409 "A run is already in progress" toast path.
- [ ] **Step 2: Run** `npm run lint` → passes.
- [ ] **Step 3: Commit** `feat(job-applies): new run checks jobs before choosing profiles`.

### Task 7: Screening report (Layout A) on the run page

**Files:**
- Create: `src/components/jobApplies/ScreeningReport.tsx`, `src/components/jobApplies/BucketJobs.tsx`
- Modify: `src/pages/JobApplyRun.tsx`, `src/pages/JobApplies.tsx` (runs list badge)

**Interfaces:**
- Consumes: Task 5 calls and helpers; existing `ProgressBar`, `Pagination`, toast pattern.
- Produces:
  - `JobApplyRun.tsx` routes by state: `phase === 'screen' && isActive(status)` → screening progress card ("Checking {fetched+failed} / {total} · {extracted} read · {closed} closed so far", Cancel); `status === 'screened'` → `<ScreeningReport runId={id} onStarted={mutateRun} />`; otherwise today's view plus a **Back to screening** toggle that renders `<ScreeningReport readOnly />`.
  - `ScreeningReport({ runId, readOnly?, onStarted })`: headline "{worth} of {total} jobs worth applying to · {check} need a check"; stacked bar (one segment per non-zero bucket, `title` = label + count); chips per non-zero bucket (click toggles `<BucketJobs runId bucket />`; the Too old chip holds the max-age select 7/14/30/60/90 → `updateJobApplyRun({ maxAgeDays })` then refetch); group cards (`locationLabel`, jobs, "N need a check", one checkbox per profile, fitting ones pre-ticked, others labelled "doesn't fit"); profiles without country show "Set this profile's country for better suggestions"; footer with per-profile job totals, per-profile resume picker (all resumes ticked by default), threshold (default 75), **Score & continue** (disabled while the request is pending, when `worth === 0`, or when no ticked profile reaches a group) → `startJobApplyRun`. Empty states per spec §5. `readOnly` hides inputs and the button.
  - `BucketJobs({ runId, bucket })`: paginated `listJobApplyRows(..., screen=bucket)`; each row shows title/company/link and `statusReason` or failing gate reasons; **Include anyway** for `clearance`/`onsite`/`too_old` (→ `setJobApplyRowInclude`, then refetch report); **Retry {n}** on `not_fetched` (→ `retryJobApplyRun`).
  - Runs list: `screened` shows a "Ready to review" badge; `phase === 'screen'` and active shows "Checking…".

- [ ] **Step 1: Implement** `ScreeningReport` and `BucketJobs`, then the page routing and badge.
- [ ] **Step 2: Run** `npm run lint` → passes.
- [ ] **Step 3: Manual check** against the running backend: create a run from a small sheet, watch progress, see the report, toggle a profile, change max age, include a job, Score & continue; double-click the button and confirm only one start request is sent (Review Focus 2).
- [ ] **Step 4: Commit** `feat(job-applies): screening report with location groups and profile picks`.

### Task 8: Results show group and assigned profiles

**Files:**
- Modify: `src/pages/JobApplyRun.tsx` (results table), `app/routers/job_applies.py` (`_RowListView` gains `screen`, `groupKey`)
- Test: `tests/test_job_applies.py`

**Interfaces:**
- Consumes: Task 4 assignments on the run.
- Produces: rows list includes `groupKey` and `screen`; the results table shows a **Location** cell (`locationLabel(groupKey)`) and the profiles assigned to that group (names from `run.profiles`, ids from `run.assignments[groupKey]` ∪ `run.assignments['*']`); `unassigned` rows show "No profile picked".

- [ ] **Step 1: Write the failing API test** `test_rows_list_includes_group`: after a started run, each listed row has `groupKey` and `screen`.
- [ ] **Step 2: Run** → FAIL. **Step 3:** add the fields to `_RowListView`. **Step 4:** run → PASS.
- [ ] **Step 5: Implement** the table cells; `npm run lint` → passes.
- [ ] **Step 6: Commit** in each repo: `feat(job-applies): results show location group and assigned profiles`.

### Task 9: End-to-end check with the real sheet

**Files:** none (verification only)

- [ ] **Step 1:** Restart the backend (no `--reload`, see `scratchpad/backend.log`) and keep Vite on :3000.
- [ ] **Step 2:** Run the 150-link sheet from run `6ac029ea…` (`sourceUrl` on that run). Expected: the run pauses at `screened`; buckets sum to 150; `closed` ≥ 31; groups include `GB`, `US`, `Worldwide`, `none`; profiles fit as their countries say.
- [ ] **Step 3:** Start with one profile per group; the run ends `done`; only assigned pairs have scores.
- [ ] **Step 4:** Report the bucket counts and any mismatch to the user before opening PRs.
