# Job Applies Flow UX Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the 3-step Job Applies flow obvious (step track, run cards, market-row profile picking with a "what this does" box, a clear step-③ strip with per-job Open/Resume/Q&A/Applied), let the sheet export run before tailoring (links filled in later), and link step ③ and Generated Resumes both ways.

**Architecture:** Backend: `google_sheets` gains read/update/append-range helpers; the export builds rows for every suggested application (placeholders for missing resumes), records where each row went, and a hook on tailored-resume completion fills the link in; the runs list adds a per-run `summary` from one aggregation; resume job dicts expose their Job Applies row/run. Frontend: new `StepTrack`, run cards on the landing page, `ScreeningReport` market rows with chip popovers, a reordered `ApplyWorkflow`, row actions in `Suggestions`, and a `?job=` deep link on Generated Resumes.

**Tech Stack:** FastAPI, Beanie/MongoDB, httpx, pytest (asyncio auto); React 18 + TypeScript + SWR + Tailwind.

**Spec:** `docs/superpowers/specs/2026-10-03-job-applies-flow-ux-design.md` (engineers_backend)

## Global Constraints

- Branch `feat/job-screening` in both worktrees (`engineers_backend-job-applies`, `engineers_app-job-applies`).
- Applying stays manual: nothing submits applications.
- Export columns unchanged: `Date, Profile, Company, Title, Job URL, Resume, Resume type, Score, Posted, Applied` (B = Profile, E = Job URL, F = Resume, G = Resume type).
- Placeholder texts: Resume "To tailor" / "Tailoring…"; Resume type "Tailored (pending)".
- A Sheets failure during link fill-in never fails the resume job.
- Step labels: ① Check jobs · ② Pick profiles · ③ Tailor & apply.
- Backend unit: `../engineers_backend/.venv/Scripts/python.exe -m pytest tests_unit -q -p no:cacheprovider --continue-on-collection-errors` (3 collection errors pre-exist on `dev`). API: `MONGODB_URI_TEST=mongodb://127.0.0.1:27018/EngineerDB_test … -m pytest tests/test_job_applies.py -o asyncio_default_fixture_loop_scope=session -o asyncio_default_test_loop_scope=session` (local mongod on 27018). Frontend: `npm run lint` (2 pre-existing errors in `InterviewPrepLibrary.tsx`).
- Commit messages `feat(job-applies): …`, ending `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.

## Review Focus

1. **Sheet rows moved or deleted before the PDF is ready** — fill-in finds the row by Job URL + Profile, else leaves the sheet alone. Tests in Task 3.
2. **Re-tailoring after export** — the newer PDF's link overwrites the same row. Test in Task 3.
3. **Exporting twice** — rows already exported (including pending ones) are not appended again. Test in Task 2.
4. **`tailorMissing` over the daily tailoring cap** — rows are still exported with "To tailor"; `overLimit` reported. Test in Task 2.
5. **Generated Resumes for a resume whose run expired** — `jobApplyRunName` null, page shows "From a Job Applies run". Test in Task 5.

---

### Task 1: Sheet range helpers

**Files:** Modify `app/google_sheets.py`; Test `tests_unit/test_google_sheets.py`

**Interfaces — Produces:**
- `append_rows(...) -> Optional[str]` returns `updates.updatedRange` (e.g. `"'2026-10-03'!A12:J20"`), None when no rows.
- `parse_row_span(a1: str) -> tuple[str, int, int]` → `(tab, first_row, last_row)`.
- `read_range(client, sheet_id, a1) -> list[list[str]]`; `update_range(client, sheet_id, a1, values: list[list[Any]]) -> None` (USER_ENTERED).

- [ ] **Step 1: Failing tests** (follow the file's mocked-`_call` / MockTransport pattern): `test_parse_row_span` (`"'2026-10-03'!A12:J20"` → `("2026-10-03", 12, 20)`; `"Sheet1!A5:J5"` → `("Sheet1", 5, 5)`); `test_append_rows_returns_range`; `test_read_and_update_range` (request method/URL/params/body asserted).
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS + unit suite.
- [ ] **Step 5: Commit** `feat(job-applies): sheet range read/update helpers`.

### Task 2: Export before tailoring

**Files:** Modify `app/routers/job_applies.py` (`ExportBody`, `export_sheet`), `app/models/job_apply.py` (`JobApplyRow.exportedRows: Dict[str, dict] = {}`); Test `tests/test_job_applies.py`

**Interfaces:**
- Consumes: Task 1 (`append_rows` range, `parse_row_span`).
- Produces: `ExportBody.tailorMissing: bool = False`; pure `export_cells(app: dict, link: Optional[str], kind: str) -> tuple[str, str]` (Resume, Resume type) giving the placeholders above; response gains `pendingLinks`, `tailorQueued`, `overLimit`; `row.exportedRows[acc] = {"sheetId", "tab", "row"}`.
- Behaviour: `needsResume` and `tailoring` applications are exported with placeholders (no longer counted as skipped); `noFile` still skipped; with `tailorMissing` the exported `needsResume` pairs are queued via the tailor-all path (`_tailor_row`, daily cap honoured), and their cell text becomes "Tailoring…".

- [ ] **Step 1: Failing API tests** (monkeypatch `google_sheets.sheet_info/ensure_tab/append_rows` to record rows and return `"'T'!A2:J9"`): `test_export_includes_rows_without_resume` (tailor-only profile → row with "To tailor" / "Tailored (pending)"; `pendingLinks` counts it); `test_export_records_rows` (`exportedRows` row numbers follow the appended range); `test_export_twice_appends_nothing_new` (Review Focus 3); `test_export_tailor_missing_queues` and `test_export_tailor_missing_over_cap` (Review Focus 4; set the cap to 0 via the existing limit setting/monkeypatch).
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** API + unit → PASS.
- [ ] **Step 5: Commit** `feat(job-applies): export to the sheet before tailoring`.

### Task 3: Fill the link in when tailoring finishes

**Files:** Create `app/job_apply/sheet_links.py`; Modify `app/routers/resume.py` (after `persist_job` on completion); Test `tests_unit/test_job_apply_sheet_links.py`, `tests/test_job_applies.py`

**Interfaces:**
- Produces: pure `find_row(values: list[list[str]], *, recorded: int, first_row: int, url: str, profile: str) -> Optional[int]` (recorded row when its B/E match, else the first matching row, else None); `async fill_link(job: ResumeApplication) -> bool` (no-op unless `job.source == "job_applies"`, `jobApplyRowId` set and the row has `exportedRows[job.accountId]`; reads `{tab}!A1:J`, writes F:G as `=HYPERLINK(link,"Download")` / "Tailored", updates `exportedRows` when the row moved; logs and returns False on any Sheets error).
- Hook: `resume.py` calls `await sheet_links.fill_link(job)` inside `try/except Exception` after the completed job is persisted.

- [ ] **Step 1: Failing unit tests:** `test_find_row_same_row`, `test_find_row_moved`, `test_find_row_gone` (Review Focus 1).
- [ ] **Step 2: Failing API tests** (mock `read_range`/`update_range`): `test_link_filled_after_tailoring` (export with a tailor-only profile, complete the ResumeApplication, call the hook → `update_range` called on the recorded row with a HYPERLINK); `test_link_refilled_after_retailor` (Review Focus 2); `test_fill_link_sheet_error_is_swallowed`.
- [ ] **Step 3: Run** → FAIL. **Step 4: Implement.** **Step 5: Run** → PASS.
- [ ] **Step 6: Commit** `feat(job-applies): fill exported sheet rows when the tailored resume is ready`.

### Task 4: Runs list summary

**Files:** Modify `app/routers/job_applies.py` (`list_runs`); Test `tests/test_job_applies.py`

**Interfaces — Produces:** each run in `GET /runs` gains `summary: {worth, closed, markets: {US, UKEU, LATAM}, toApply, applied, tailoring}`, from one `$match {runId: {$in: ids}}` aggregation over rows (`screen`, `groupKey`, applied marks, suggested = `topScore >= threshold` or tailoring/tailorOnly as `_rows_query(view="suggested")` defines) plus one ResumeApplication count of queued/in-progress by `jobApplyRowId`. Markets per job via `markets_for`. If the aggregation fails, `summary` is null for every run (logged); the list still returns.

- [ ] **Step 1: Failing API test** `test_runs_list_summary`: a screened run → `worth`, `closed`, `markets.US`; a started+scored run with one applied mark → `toApply`, `applied`.
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(job-applies): runs list carries a per-run summary`.

### Task 5: Resume jobs know their Job Applies run

**Files:** Modify `app/routers/resume.py` (`_job_to_dict`, list + detail); Test: the resume API tests file (search `tests/` for `/resume/jobs`), or `tests/test_job_applies.py`

**Interfaces — Produces:** `jobApplyRowId`, `jobApplyRunId`, `jobApplyRunName` (null when the row/run is gone; list endpoint looks rows/runs up in one batch per page).

- [ ] **Step 1: Failing API tests:** `test_resume_job_links_to_run`; `test_resume_job_run_expired` (row deleted → ids/name null, Review Focus 5).
- [ ] **Step 2: Run** → FAIL. **Step 3: Implement.** **Step 4: Run** → PASS.
- [ ] **Step 5: Commit** `feat(job-applies): resume jobs link back to their run`.

### Task 6: Step track + landing run cards

**Files:** Create `src/components/jobApplies/StepTrack.tsx`, `src/components/jobApplies/RunCard.tsx`; Modify `src/pages/JobApplies.tsx`, `src/pages/JobApplyRun.tsx` (header), `src/api/endpoints.ts` (`JobApplyRun.summary`)

**Interfaces — Produces:** `StepTrack({ current, hint?, large? })`; `runStep(run) -> 1|2|3|'done'` and `runGroup(run) -> 'needs'|'progress'|'finished'` in `format.ts` (rules: spec §2–§3); `RunCard({ run, onDelete })` with the per-step numbers and button texts of spec §3.

- [ ] **Step 1: Implement** (a run with `summary: null` renders its card without numbers) StepTrack (large variant with the three sentences of spec §2), "+ New run" toggle (open when no runs), grouped cards, run-page header track.
- [ ] **Step 2:** `npm run lint` → only the pre-existing errors.
- [ ] **Step 3: Commit** `feat(job-applies): step track and run cards on the landing page`.

### Task 7: Step ② market rows

**Files:** Modify `src/components/jobApplies/ScreeningReport.tsx`; Create `src/components/jobApplies/MarketRow.tsx`

**Interfaces — Produces:** `MarketRow({ group, profiles, picked, usable, onAdd, onRemove, unchecked, onToggleResume, readOnly })` (chips with popover holding `ResumeChecklist`, "+ add" menu fitting-first); `scorePlan(report, picks, options) -> { pairs: number, lines: string[], warnings: string[] }` (pure, in `ScreeningReport.tsx` or `format.ts`; dedupes a job counted in two markets for the same profile).

- [ ] **Step 1: Implement** market rows replacing the market cards and the per-profile footer; "What 'Score N applications' does" box from `scorePlan`; button "Score N applications →"; per-profile counts deduplicated.
- [ ] **Step 2:** `npm run lint`. **Step 3: Commit** `feat(job-applies): pick profiles per market with a clear "what this does"`.

### Task 8: Step ③ strip, row actions, export dialog

**Files:** Modify `src/components/jobApplies/ApplyWorkflow.tsx`, `Suggestions.tsx`, `ExportSheetDialog.tsx`, `src/api/endpoints.ts` (`JobApplyExportResult.pendingLinks/tailorQueued/overLimit`, `exportJobApplySheet` body `tailorMissing`)

**Interfaces — Consumes:** Task 2 response fields; Task 5 ids.
**Produces:** strip order Export → Tailor N → Apply (texts of spec §5.1; Export enabled when any suggested application is not exported); per-application ↗ Open, ↓ Resume, 💬 Q&A (`/resume/generated?job=<tailoredJobId>`, new tab), Applied; export dialog checkbox "Tailor the N jobs without a resume now" (default on) and "M links fill in when ready".

- [ ] **Step 1: Implement.** **Step 2:** `npm run lint`. **Step 3: Commit** `feat(job-applies): clear step-3 actions and export before tailoring`.

### Task 9: Generated Resumes deep link and link back

**Files:** Modify `src/pages/Generated.tsx`, `src/api/endpoints.ts` (`ResumeJob.jobApplyRowId/jobApplyRunId/jobApplyRunName`)

**Produces:** `?job=<id>` opens that job's drawer on load (fetch `GET /resume/jobs/{id}` when not on the page; toast "That resume isn't available" on 404); Job Applies resumes show "From run: {name} ↗" → `/job-applies/{runId}`, or "From a Job Applies run" when the run is gone.

- [ ] **Step 1: Implement.** **Step 2:** `npm run lint`. **Step 3: Commit** `feat(job-applies): Generated Resumes links to and from Job Applies`.

### Task 10: Browser check on the isolated stack

**Files:** none

- [ ] **Step 1:** Start the isolated stack (backend :8081 on `EngineerDB_e2e`, `JOB_APPLY_WORKER_DISABLED` unset, Vite :3001), seed user `e2e@example.com`.
- [ ] **Step 2:** Walk: landing (no runs → large steps + open form; with runs → groups/buttons), run a sheet to step ②, market chips/popover/"what this does", Score, step ③ strip and row actions, export dialog (Google Sheets mocked or skipped if no service account — note which), 💬 Q&A opens the drawer, link back to the run. Screenshot each.
- [ ] **Step 3:** Report results and any mismatch before opening PRs.
