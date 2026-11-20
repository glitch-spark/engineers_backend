# Applications Insights — Slice 1 (Applications table) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A precomputed `applications` collection (one row per identifiable application with its interview outcome), kept fresh by the cron, a backfill script, the team response window, and an admin coverage endpoint.

**Architecture:** `app/applications/` holds pure row-building and matching (`rows.py`), the sync that writes the collection (`sync.py`), and the response window (`window.py`). The keen-spirit cron calls the sync every tick; a daily full rebuild cleans up deletes. `GET /stats/applications/coverage` (admin) reports coverage and the window.

**Tech Stack:** FastAPI + Beanie + Motor (mongomock tests).

**Spec:** `docs/superpowers/specs/2026-11-19-applications-insights-design.md` (sections "Data model", "The response window", "Decisions from the builder check").

**Repo / branch:** backend `/home/eningeer/work/Engineer/engineers_backend` on `feat/stats-insights` (spec commit 282b6c4). Backend only.

## Global Constraints

- Row key: `(accountId, jobKey)` with `jobKey = app.models.resume_application.job_url_key(jobUrl)`; rows without a job link keyed by `src:<collection>:<id>`.
- Sources: `Bid` (kind bidder → `by: "bidder"`, kind self → `"self"`), `ResumeApplication` with `countsAsBid != False` (`by: "self"` unless a bidder bid shares the row → `"bidder"`). `appliedAt` = earliest source time (Bid.firstAt / ResumeApplication.createdAt, naive UTC).
- Interview match order: `resumeLink.resumeJobId` → that resume's row; else `(interview.accountId, job_url_key(interview.jobUrl))`; else unmatched.
- Stage buckets via `interview_rounds._breakdown_group`; AI interviews / home assessments ignored; canceled rounds don't count; an interview with only canceled rounds isn't an interview. `daysToInterview` = whole days from `appliedAt` to the first non-canceled round's date (≥ 0).
- Response window: computed on read over matched rows with `appliedAt` in the last 90 days; median + 90th percentile of `daysToInterview`; < 30 matched → `{median: None, p90: 14, thin: True}`; matured cutoff = max(7, p90) days.
- Backfill from 2026-08-01; dry run by default, `--apply` writes. All writes idempotent (upserts on the row key).
- Never run anything against the real DB in tests or during implementation (backend `.env` is production). Scripts default to dry run.
- Commits single line, no co-author / "Generated with" trailers.
- Tests: `.venv/bin/python -m pytest -q -p no:cacheprovider tests_unit/<file>`; full suite with `--ignore=tests_unit/test_resume_experience_roles.py --ignore=tests_unit/test_style_serializer.py --ignore=tests_unit/test_template_annotate_removals.py` (2 known local failures).

## Review Focus

1. Same job applied by a bidder and with a tailored resume on the same profile → one row, `by: "bidder"`, earliest `appliedAt` (Task 1).
2. Interview linked to a resume of a *different* profile's row, or job link with tracking params → still matches the right row (Task 1).
3. Running the sync twice changes nothing; a deleted bid disappears after the daily rebuild (Task 2).
4. Rejected (Bid Track review) bids stay as rows with `bidStatus: "rejected"` (excluded later from conversion) (Task 1).
5. Thin data → window falls back to 14 days, never below 7 (Task 3).

---

### Task 1: Model + pure row building and matching

**Files:** Create `app/models/application.py` (Beanie `Application`, collection `applications`, indexes: unique `key`; `(ownerId, appliedAt)`; `(accountId, appliedAt)`; `interviewId`), register it in `app/db.py` `DOCUMENT_MODELS`; Create `app/applications/__init__.py`, `app/applications/rows.py`. Test `tests_unit/test_applications_rows.py`.

**Interfaces (produces):**
- `Application` fields: `key: str`, `accountId: Optional[ObjectId]`, `ownerId: ObjectId`, `region: Optional[str]`, `jobKey: Optional[str]`, `board: Optional[str]`, `appliedAt: datetime`, `by: Literal["self","bidder"]`, `bidderId: Optional[ObjectId]`, `bidStatus: Optional[str]`, `score: Optional[int]`, `scoreBand: Optional[str]`, `promptId/promptName/templateName: Optional[str]`, `interviewId: Optional[ObjectId]`, `firstRoundAt: Optional[datetime]`, `daysToInterview: Optional[int]`, `furthest: Optional[str]`, `result: Optional[str]`, `sources: list[str]`, `syncedAt: datetime`.
- `board_of(url: Optional[str]) -> Optional[str]` — registrable domain (`jobs.ashbyhq.com` → `ashbyhq.com`, `acme.wd5.myworkdayjobs.com` → `myworkdayjobs.com`, `boards.greenhouse.io` → `greenhouse.io`); keep two labels, or three for known 2-part suffixes (`co.uk`, `com.au`, …).
- `build_rows(bids: list[dict], resumes: list[dict], regions: dict) -> dict[str, dict]` — merge by key per the constraints.
- `interview_outcome(iv) -> Optional[dict]` → `{firstRoundAt, furthest, result}` or None (no countable round).
- `match_interviews(rows: dict[str, dict], interviews: list, resume_row: dict[str, str]) -> tuple[int, int]` — sets `interviewId`, `firstRoundAt`, `daysToInterview`, `furthest`, `result` on matched rows (earliest interview wins when two match one row); returns (matched, unmatched). `resume_row` maps resume id → row key.

- [ ] **Step 1: Failing tests** — `test_board_of_examples` (ashby, greenhouse, workday, lever, linkedin, `None`); `test_bidder_and_resume_merge_one_row_bidder_wins`; `test_rows_without_link_keyed_by_source`; `test_rejected_bid_kept_with_status`; `test_counts_as_bid_false_skipped`; `test_match_by_resume_link_then_job_key`; `test_job_link_with_tracking_params_matches` (relies on `job_url_key`); `test_days_to_interview_first_non_canceled_round`; `test_only_canceled_rounds_not_an_interview`; `test_furthest_and_result`.
- [ ] **Step 2: Run** — FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** — PASS.
- [ ] **Step 5: Commit** `feat: applications rows — merge bids and tailored resumes per job and match interviews`

---

### Task 1b: Job title, job area and profile age on rows

**Files:** Create `app/applications/area.py`; Modify `app/applications/rows.py`, `app/models/application.py` (fields `title: Optional[str]`, `area: Optional[str]`, `profileAgeDays: Optional[int]`). Test `tests_unit/test_applications_area.py` (+ rows tests).

**Interfaces:**
- `job_title(job_description: Optional[str], job_key: Optional[str]) -> Optional[str]` — first non-empty line of the description (trimmed, ≤ 120 chars); else the last `jobKey` segment after its first `_`, as words, only when it contains a letter-word of ≥ 3 letters that isn't an id (uuid / digits / `r-14343`-style); else None.
- `classify_area(title: Optional[str]) -> Optional[str]` — case-insensitive keyword rules, first match wins in this order: QA (qa, quality, test, sdet) → DevOps (devops, sre, site reliability, platform, infrastructure, cloud) → AI / ML (ai, ml, machine learning, llm, genai, data scien) → Data (data engineer, analytics, etl, bi ) → Mobile (ios, android, mobile, react native, flutter) → Full stack (full stack, fullstack, full-stack) → Frontend (front end, frontend, front-end, react, angular, vue, ui ) → Backend (back end, backend, back-end, python, java, golang, go , node, .net, ruby, php, api) → Other. None title → None.
- `build_rows(bids, resumes, accounts)` — `accounts` now `{accountId: {"region", "createdAt"}}` (was regions); rows get `title` / `area` (from the earliest resume when one exists, else from the bid's jobKey) and `profileAgeDays`.

- [ ] **Step 1: Failing tests** — `test_job_title_from_description_first_line`; `test_job_title_from_readable_job_key` (`netweb_backend-ai-engineer` → "backend ai engineer"); `test_job_title_none_for_id_keys` (`jobs_eaa48261-…`, `halcyon_6252121004`, `caresource_r-14343`); `test_classify_area_examples` (Senior Backend Developer → Backend; Full Stack Engineer → Full stack; ML Engineer → AI / ML; Site Reliability Engineer → DevOps; QA Automation → QA; React Developer → Frontend; iOS Engineer → Mobile; Product Manager → Other; None → None); `test_row_area_prefers_resume_title`; `test_profile_age_days`.
- [ ] **Step 2–4:** FAIL → implement → run files + full suite.
- [ ] **Step 5: Commit** `feat: applications rows carry job title, job area and profile age`

---

### Task 2: Sync (incremental + daily full rebuild), cron hook, backfill script

**Files:** Create `app/applications/sync.py`, `scripts/build_applications.py`; Modify `app/alerts_tick.py` (call `run_applications_tick()` after the existing ticks, own try/except + log like the others). Test `tests_unit/test_applications_sync.py`.

**Interfaces:** (accounts map `{id: {region, createdAt}}` from Task 1b)
- `async def full_rebuild(since: date, *, apply: bool) -> dict` — loads bids (`firstAt ≥ since`), resumes (`createdAt ≥ since`, `countsAsBid != False`), interviews (`createdAt ≥ since` or any round dated ≥ since), builds + matches, upserts every row and deletes rows (with `appliedAt ≥ since`) whose key no longer exists; returns counts `{rows, matched, unmatched, noLink, deleted}`. Dry run returns counts without writing.
- `async def incremental(checkpoint: datetime) -> dict` — rebuilds only the keys touched by bids / resumes / interviews changed since `checkpoint` (`updatedAt` or creation time), re-matching interviews changed since then; upserts those rows.
- `async def run_applications_tick(now: Optional[datetime] = None) -> dict` — reads `app_settings` doc `{"_id": "applications_sync"}` (`checkpoint`, `lastFull`); runs `full_rebuild(date(2026, 8, 1), apply=True)` when `lastFull` is missing or > 24 h old, else `incremental(checkpoint)`; saves the new checkpoint (start time of this run). Skips (returns `{"skipped": "disabled"}`) when env `APPLICATIONS_SYNC_DISABLED=1`.
- Script: `python -m scripts.build_applications [--since YYYY-MM-DD] [--apply]` → prints `full_rebuild` counts.

- [ ] **Step 1: Failing tests** — `test_full_rebuild_idempotent`; `test_dry_run_writes_nothing`; `test_deleted_bid_removed_on_full_rebuild`; `test_incremental_adds_new_bid_and_matches_new_interview`; `test_relinked_interview_moves_match`; `test_tick_runs_full_first_then_incremental`; `test_tick_disabled_env`.
- [ ] **Step 2–4:** FAIL → implement → run file + full suite.
- [ ] **Step 5: Commit** `feat: applications sync (cron incremental + daily rebuild) and backfill script`

---

### Task 3: Response window + admin coverage endpoint

**Files:** Create `app/applications/window.py`; Modify `app/routers/stats.py` (add `GET /stats/applications/coverage`, admin only — 403 for others). Test `tests_unit/test_applications_window.py` (+ router test).

**Interfaces:**
- `async def response_window(today: date) -> dict` → `{median, p90, matured_days, n, thin}` per the constraints; cached in-process for 10 minutes keyed by `today` (module dict with timestamp; tests can clear it).
- `GET /stats/applications/coverage?since=YYYY-MM-DD` → `{rows, matched, unmatchedInterviews, noLink, byBoard: [{board, rows}] (top 15), window, lastSync: {checkpoint, lastFull}}`.

- [ ] **Step 1: Failing tests** — `test_window_median_p90`; `test_window_thin_fallback`; `test_matured_never_below_7`; `test_cache_reused_within_10_minutes`; router `test_coverage_admin_only`, `test_coverage_counts`.
- [ ] **Step 2–4:** FAIL → implement → run file + full suite.
- [ ] **Step 5: Commit** `feat: response window and admin applications coverage endpoint`
