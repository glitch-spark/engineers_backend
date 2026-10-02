# Job Applies — Tailored Resumes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** From a Job Applies run, tailor a resume for any job through a shared, restart-safe and capped generation
queue, and record exactly which file (uploaded or tailored) each application used, with bids counted only once
applied.

**Architecture:**
- **Queue:** `ResumeApplication` becomes a queue. `app/resume_queue.py` holds the enqueue function and a lease-based
  worker that runs the existing `_run_job` with bounded concurrency; the Generator page uses it too.
- **Job Applies:** rows link to a tailored job, and applied marks name either an uploaded resume or a tailored job.
- **Bids:** aggregations skip `countsAsBid: false`.

**Tech Stack:** FastAPI, Beanie/Motor, existing `resume_service` (LLM → Playwright → S3); React 18, SWR.

**Spec:** `docs/superpowers/specs/2026-10-01-job-applies-tailored-resumes-design.md`

## Global Constraints

- **Settings:** `RESUME_LLM_CONCURRENCY` default 3, `RESUME_RENDER_CONCURRENCY` default 2, `RESUME_DAILY_CAP`
  default 300. Disable the queue worker with `RESUME_QUEUE_DISABLED=1`, which `tests/conftest.py` sets.
- **Worker timing:** lease heartbeat 30 s, stale after 3 min, sweep every 2 min, max attempts 3, wake poll 5 s.
- **S3 key:** `{YYYY_MM_DD}/{profile}/{company}/{jobId}/Resume.pdf`.
- **Bid filter:** `{"countsAsBid": {"$ne": False}}`. Absent or `None` counts.
- **Log `fileKey`:** `resumeId`, or `"tailored:" + tailoredJobId`. The unique index is `user_url_file` on
  `(userId, url, fileKey)`, replacing `user_url_resume`.
- **Tests:**
  - Backend unit tests: `../engineers_backend/.venv/Scripts/python -m pytest tests_unit -q`.
  - API tests on mongomock: `PYTHONPATH=<scratchpad>/mockmongo … -p mockmongo_plugin`, or the scratchpad `suite.sh`.
  - Frontend: `tsc-new-errors.sh` plus `vite build`.
- **Commits:** end every commit message with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. **Server restart mid-batch:** jobs `in_progress` with a stale lease must go back to the queue, and nothing may run
   twice concurrently. Task 2 (`test_stale_lease_requeued_once`).
2. **Two tailor clicks on the same row in quick succession** must not leave two queued jobs linked or orphaned.
   Task 5 (`test_retailor_replaces_queued_job`).
3. **Marking applied with a tailored resume that's still generating or failed** must be rejected with a clear 400.
   Task 6 (`test_mark_tailored_requires_completed_pdf`).
4. **The daily cap across both pages:** Generator-page generations count toward the same cap. Task 3
   (`test_cap_counts_both_sources`).
5. **Existing applied-log entries** (from before `fileKey`) must keep blocking duplicates and still appear as
   "Already applied". Task 6 (`test_log_backfill_filekey`).

---

## File Structure

Backend:

| File | Change |
|---|---|
| `app/models/resume_application.py` | Fields `source`, `countsAsBid`, `leaseAt`, `attempts`, `promptOverride`, `jobApplyRowId`; index `(status, createdAt)`; `BID_FILTER` constant |
| `app/resume_queue.py` (new) | `enqueue_generation`, `daily_count`, worker (`start_queue`, `_claim_one`, `_requeue_stale`), `RUNNER` hook |
| `app/routers/resume.py` | `/generate` and `/retry` go through the queue; `_run_job` reads `promptOverride`; `run_job_doc(job)` adapter |
| `app/resume_service.py` | `generate_resume(..., job_id=None)` → unique S3 key; render semaphore around `_html_to_pdf` |
| `app/config.py` | Three settings above |
| `app/main.py` | Start the queue loop in lifespan (unless disabled) |
| `app/routers/metrics.py`, `app/alerts_service.py` | Add `BID_FILTER` to every ResumeApplication match |
| `app/models/job_apply.py` | `JobApplyRow.tailoredJobId/tailoredAccountId`; `AppliedMark.resumeId` optional plus `tailoredJobId`; log `fileKey`, `tailoredJobId`; unique index `user_url_file` |
| `app/db.py` | `STALE_INDEXES["job_application_log"] = ["user_url_resume"]`; migration back-filling `fileKey` |
| `app/routers/job_applies.py` | `tailor`, `tailor-all`; tailored status, `otherResumes`, run `tailoring` counts; marks of both kinds; mark-top prefers tailored |
| `tests/test_resume_queue.py` (new), `tests/test_bid_counting.py` (new), `tests/test_job_applies.py` | Tests |

Frontend:

| File | Change |
|---|---|
| `src/api/endpoints.ts` | Types and calls: tailor, tailorAll, mark body with `tailoredJobId`, row `tailored`/`otherResumes`, run `tailoring` |
| `src/components/jobApplies/Suggestions.tsx` | Tailored line (Tailor ▾ / status / applied / download / re-tailor) and the "Other resume…" picker |
| `src/pages/JobApplyRun.tsx` | Keys `t`/`d`/`a`, "Tailor all to apply (N)" dialog, header progress, polling while tailoring |
| `src/components/jobApplies/RunSummary.tsx` | "Tailoring 12 / 40" |
| `src/pages/Generated.tsx` | "Job Applies" tag; skip auto-download for `source="job_applies"` |

---

### Task 1: Queue fields, enqueue, unique S3 key, render limit

**Files:** `app/models/resume_application.py`, `app/resume_queue.py`, `app/routers/resume.py`,
`app/resume_service.py`, `app/config.py`; test `tests/test_resume_queue.py`.

**Interfaces — produces:**
- `ResumeApplication` fields from spec §3.1. `BID_FILTER: dict` is in the same module.
- `async enqueue_generation(*, user: UserExtra, account: Account, jd: str, company: str, job_url: str | None, questions: list[str], prompt_body: str | None, cover_letter: bool, source: Literal["generator", "job_applies"] = "generator", job_apply_row_id: PydanticObjectId | None = None) -> ResumeApplication`.
  It validates exactly as `/generate` did (raising the same `HTTPException`s), inserts with `status="queued"`,
  `countsAsBid=False if source == "job_applies" else None`, `promptOverride=prompt_body`, then calls `wake()`.
- `wake() -> None` sets the module `asyncio.Event`.
- `run_job_doc(job: ResumeApplication) -> Awaitable[None]` (in the `resume.py` router) calls `_run_job` with the
  job's stored fields and `prompt_body_override=job.promptOverride`.
- `generate_resume(account, jd, company, prompt_body=None, owner=None, job_id: str | None = None)` builds the key
  `{date}/{profile}/{company}/{job_id}/Resume.pdf` when `job_id` is given, and `_html_to_pdf` runs under
  `asyncio.Semaphore(settings.RESUME_RENDER_CONCURRENCY)`.
- `/generate` returns 202 `{jobId, status: "queued"}` without creating a task. `/retry` resets the fields, sets
  `status="queued"`, keeps `promptOverride`, and calls `wake()`.

- [ ] **Step 1: Write the failing tests** (`tests/test_resume_queue.py`, mongomock):
  - `test_generate_enqueues_without_running`: POST `/resume/generate` → 202. The document is `queued` with
    `source="generator"` and `countsAsBid is None`, and no task ran (the stub `RUNNER` records nothing).
  - `test_retry_keeps_prompt_override`: a failed job with `promptOverride="X"` → POST retry → `queued`, still `"X"`.
  - `test_unique_s3_key`: with `_maybe_upload_to_s3` called with `job_id="abc"`, the key ends with
    `/abc/Resume.pdf`. This is a unit-level call with the boto client monkeypatched.
- [ ] **Step 2: Run** the tests. Expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the tests plus the existing `tests/test_*` touching resume. Expect PASS.
- [ ] **Step 5: Commit** `feat(resume): persistent generation queue fields and enqueue; unique S3 key`.

### Task 2: Queue worker

**Files:** `app/resume_queue.py`, `app/main.py`; test `tests/test_resume_queue.py`.

**Interfaces — produces:**
- `RUNNER: Callable[[ResumeApplication], Awaitable[None]] = run_job_doc` (a module attribute that tests replace).
- `async _claim_one() -> ResumeApplication | None` does the atomic `find_one_and_update` from spec §3.3: oldest
  `queued` → `in_progress`, `$inc attempts`, sets `leaseAt` and `startedAt`.
- `async _requeue_stale(now: datetime) -> int`:
  - `in_progress` with `leaseAt` older than 3 min (or null) and `attempts < 3` → `queued`
  - the same with `attempts >= 3` → `failed`, "Generation was interrupted repeatedly"
- `async run_queue_once() -> int` claims up to `RESUME_LLM_CONCURRENCY - running` jobs and starts them with a
  heartbeat. It returns the number started. It is used by the loop and by tests.
- `async queue_forever()` sweeps with `_requeue_stale` every 2 min and calls `run_queue_once` on wake or every 5 s.
- `start_queue()` is called from the lifespan unless `RESUME_QUEUE_DISABLED=1`.

- [ ] **Step 1: Write the failing tests:**
  - `test_concurrency_limit`: 5 queued jobs with `RESUME_LLM_CONCURRENCY=2` and a RUNNER that waits on an Event.
    After `run_queue_once`, exactly 2 are `in_progress`. Releasing them and running again starts the next 2.
  - `test_stale_lease_requeued_once`: an `in_progress` job with `leaseAt` 10 min ago, `attempts=1` →
    `_requeue_stale` makes it `queued`. With `attempts=3` it becomes `failed`. A fresh lease is untouched.
  - `test_claim_is_exclusive`: two concurrent `_claim_one()` calls over one queued job → one gets it, the other
    gets `None`.
- [ ] **Step 2: Run** the tests. Expect FAIL.
- [ ] **Step 3: Implement.** RUNNER runs inside a task that updates `leaseAt` every 30 s and catches every
  exception. `_run_job` already marks the job failed.
- [ ] **Step 4: Run** the tests. Expect PASS.
- [ ] **Step 5: Commit** `feat(resume): lease-based generation worker with bounded concurrency`.

### Task 3: Daily cap

**Files:** `app/resume_queue.py`; test `tests/test_resume_queue.py`.

**Interfaces — produces:**
- `async daily_count(user: UserExtra) -> int` counts ResumeApplications created today in `APP_TIMEZONE`, from
  both sources. It reuses `alerts_service.local_day_bounds_utc` if importable, otherwise its own helper.
- `enqueue_generation` raises `HTTPException(429, "Daily limit of {cap} tailored resumes reached. It resets at midnight.")`
  when `daily_count >= RESUME_DAILY_CAP`.
- `remaining_today(user) -> int` is exposed for tailor-all.

- [ ] **Step 1: Write the failing tests:**
  - `test_cap_blocks_single_enqueue`: cap=2, with 2 documents today → POST `/resume/generate` returns 429 with the
    message.
  - `test_cap_counts_both_sources`: 1 `generator` and 1 `job_applies` document today with cap=2 → the next one is
    blocked.
- [ ] **Step 2: Run** the tests. Expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the tests. Expect PASS.
- [ ] **Step 5: Commit** `feat(resume): per-user daily generation cap`.

### Task 4: Bid counting

**Files:** `app/routers/metrics.py`, `app/alerts_service.py`; test `tests/test_bid_counting.py`.

**Interfaces:**
- **Consumes:** `BID_FILTER` from Task 1.
- **Produces:**
  - Every ResumeApplication `find` and `$match` in `metrics.py` includes `BID_FILTER`: `_bid_count`, the
    ResumeApplication call of `_bucketed_count` (new keyword `extra: dict | None = None` merged into `$match`),
    `_bids_by_user`, `bid_pipe` in `_trend_by_user`, and the `dashboard_feed` bids query.
  - `alerts_service._tick_daily_bids`'s count does the same.

- [ ] **Step 1: Write the failing tests:** `test_unapplied_tailored_jobs_are_not_bids`. Insert 3 documents for a
  user today: one with `countsAsBid` absent, one with `True`, one with `False`. Then:
  - `GET /metrics/dashboard` KPI bids = 2
  - `_bids_by_user` = 2
  - `_bid_count` = 2
- [ ] **Step 2: Run** the test. Expect FAIL (3).
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the test, plus `tests_unit/test_alerts*.py` and `test_dashboard_stats.py`. Expect PASS.
- [ ] **Step 5: Commit** `feat(metrics): bids skip tailored resumes not yet applied with`.

### Task 5: Tailor endpoints and tailored status on rows

**Files:** `app/models/job_apply.py`, `app/routers/job_applies.py`; test `tests/test_job_applies.py`.

**Interfaces:**
- **Consumes:** `enqueue_generation`, `remaining_today`.
- **Produces:**
  - `JobApplyRow.tailoredJobId: PydanticObjectId | None`, `tailoredAccountId: str | None`.
  - `POST /job-applies/rows/{rowId}/tailor {accountId?, coverLetter?}` returns `{tailored}`. The default profile is
    the best `scores` total for the row; a profile outside `run.selection` gives 400; no `jdText` gives 400; the cap
    gives 429. Replacement follows spec §5.1: an old job that is `queued` and unused by any mark is deleted.
  - `POST /job-applies/runs/{runId}/tailor-all {accountId?, coverLetter?}` returns `{queued, skippedCap}`. It
    covers rows from `_rows_query(view="suggested", applied="no")` whose tailored job is missing or failed.
  - Rows list and detail gain:
    - `tailored: {jobId, accountId, status, step, hasPdf, error} | None`, from one ResumeApplication `$in` query
      per page
    - `otherResumes: [{accountId, resumeId, filename, total}]`, every `scores` entry not in `suggestions`, sorted by
      total
  - Run detail gains `tailoring: {queued, inProgress, ready, failed}` for jobs with `jobApplyRowId` in the run.
- [ ] **Step 1: Write the failing tests** (the queue is disabled, so jobs stay queued and tests flip status
  directly):
  - `test_tailor_defaults_to_best_profile`
  - `test_tailor_rejects_profile_outside_run`
  - `test_retailor_replaces_queued_job`: tailor twice → one queued document; the row points to the second
  - `test_tailor_all_respects_cap`: cap leaves 1 → `{queued: 1, skippedCap: 1}`
  - `test_rows_show_tailored_status_and_other_resumes`
- [ ] **Step 2: Run** the tests. Expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the tests. Expect PASS.
- [ ] **Step 5: Commit** `feat(job-applies): tailor a job's resume, one or all, with status on rows`.

### Task 6: Marks name the file; log fileKey; bids flip

**Files:** `app/models/job_apply.py`, `app/db.py`, `app/routers/job_applies.py`; test `tests/test_job_applies.py`.

**Interfaces — produces:**
- **`AppliedMark`:**
  - `resumeId: str | None = None`, `tailoredJobId: str | None = None`.
  - The helper `mark_file_key(m) -> str` gives `resumeId` or `"tailored:" + tailoredJobId`.
- **`JobApplicationLog`:** adds `fileKey: str`, `tailoredJobId: str | None`, `resumeId: str | None`, and the unique
  index `user_url_file`.
- **`app/db.py`:**
  - `STALE_INDEXES["job_application_log"] = ["user_url_resume"]`
  - `_migrate_log_filekey` sets `fileKey = resumeId` where `fileKey` is missing, before `init_beanie`
- **`PUT /rows/{id}/applied`:**
  - The body is `{accountId, resumeId?, tailoredJobId?, applied}`, with exactly one of the two ids (else 400).
  - A tailored mark requires `tailoredJobId == row.tailoredJobId` and a `completed` job with a PDF (else 400), and
    sets that document's `countsAsBid` to True, or False on unmark.
- **`_set_applied`:** takes `file: {resumeId} | {tailoredJobId}` and keys the log by `fileKey`.
- **`mark-top-applied`:** uses the row's completed tailored job when one exists, otherwise the top suggestion.
  `marks` items carry whichever id was used. `unmark-applied` accepts both.
- [ ] **Step 1: Write the failing tests:**
  - `test_mark_tailored_requires_completed_pdf`
  - `test_mark_tailored_counts_bid_and_unmark_removes_it`
  - `test_mark_other_uploaded_resume`: a resume below the threshold, from `otherResumes`, can be marked
  - `test_mark_top_prefers_ready_tailored`
  - `test_log_backfill_filekey`: an old log document without `fileKey` → after the migration has a `fileKey`
    and still shows as `previouslyApplied`
- [ ] **Step 2: Run** the tests. Expect FAIL.
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run** the full backend suite (`suite.sh`) plus `tests/test_accounts.py`. Expect PASS.
- [ ] **Step 5: Commit** `feat(job-applies): applied marks name the uploaded or tailored file; bids on apply`.

### Task 7: Frontend API

**Files:** `src/api/endpoints.ts`.
**Interfaces — produces:**
- **Types:**
  - `JobApplyTailored {jobId, accountId, status: ResumeJobStatus, step, hasPdf, error?}`
  - `JobApplyOtherResume {accountId, resumeId, filename, total}`
  - `JobApplyRow.tailored`, `JobApplyRow.otherResumes`
  - `JobApplyRun.tailoring {queued, inProgress, ready, failed}`
  - `JobApplyAppliedMark.resumeId?`, `.tailoredJobId?`
- **Calls:**
  - `tailorJobApplyRow(rowId, {accountId?, coverLetter?})`
  - `tailorAllJobApplies(runId, {accountId?, coverLetter?})`
  - `setJobApplyResumeApplied(rowId, {accountId, resumeId?, tailoredJobId?, applied})`
- **Downloads:** a tailored PDF goes through the existing `downloadResumeJob` / `GET /resume/jobs/{id}/download?format=pdf`.
- [ ] **Step 1: Implement.** **Step 2:** run `tsc-new-errors.sh` (fix the callers it flags). **Step 3:** commit.

### Task 8: Apply-with cell, keyboard, batch, progress

**Files:** `Suggestions.tsx`, `JobApplyRun.tsx`, `RunSummary.tsx`.
**Behaviour (spec §6):**
- **Tailored line:**
  - **Tailor ▾:** a menu of the run's profiles, sorted by this row's best score per profile, with the first as
    the default.
  - **Status:** Queued / Generating… (spinner) / Tailored · profile, with an applied checkbox, a download and
    Re-tailor ▾. On failure: Failed: retry.
- **Other resume…:** a menu of `otherResumes`. Picking one marks it applied, optimistically, with Undo.
- **Keyboard:**
  - `t`: tailor the highlighted job with the default profile
  - `d`: download the tailored PDF if it's ready, otherwise the top suggestion's uploaded PDF
  - `a`: mark applied with the tailored resume if it's ready, otherwise the top suggestion, then move on
- **"Tailor all to apply (N)":** in the action bar when the status filter is To apply. A dialog shows N, the daily
  cap left and a cover-letter checkbox; the result goes in a toast (`queued`, `skippedCap`).
- **Progress:** `RunSummary` shows "Tailoring X / Y" while `queued + inProgress > 0`.
- **Polling:** the run and rows refresh every 3 s while any tailored job is pending.
- [ ] **Step 1: Implement.** **Step 2:** run `tsc-new-errors.sh` and `vite build`. **Step 3:** commit.

### Task 9: Generated page

**Files:** `src/pages/Generated.tsx`, `src/api/endpoints.ts` (`ResumeJob.source?`); backend `_job_to_dict` returns
`source`.
- [ ] **Step 1:** add the "Job Applies" tag in the row. The auto-download effect skips `source === 'job_applies'`.
- [ ] **Step 2:** run the backend API test asserting `GET /resume/jobs` includes `source`, plus `tsc` and build.
- [ ] **Step 3:** commit.

### Task 10: Final verification and review

- [ ] Run the full backend suite and the frontend build. Restart the backend and smoke the new routes (401
  without a token).
- [ ] Request a whole-branch code review (superpowers:requesting-code-review) and fix the Critical and Important
  findings with failing-then-passing tests.
