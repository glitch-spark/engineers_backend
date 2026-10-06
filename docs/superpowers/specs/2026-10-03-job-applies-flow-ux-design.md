# Job Applies — make the 3-step flow obvious

Date: 2026-10-03
Branches: `feat/job-screening` in `engineers_backend` and `engineers_app` (builds on the screening work there)
Status: approved design, pending spec review
Builds on: `2026-10-02-job-applies-screening-design.md`

## 1. Goal

Job Applies is three steps: ① check job URLs, ② pick profiles, ③ get suggestions, tailor and apply. Today a new
user can't see that from the screens: the runs list doesn't say which step a run is at, step ② asks for profiles
twice (market cards, then a resume list) without saying what "Score & continue" does, step ③ can't export to the
team sheet until every resume is tailored, and a tailored resume in Generated Resumes has no link back to its job
(nor the other way), so screening Q&A for a job is hard to find.

Applying itself stays manual.

### Decisions already made

| Topic | Decision |
|---|---|
| Steps | Keep the 3 steps; show them on the landing page and the run page. |
| Landing | Layout A: "+ New run" button; runs as cards grouped Needs you / In progress / Finished, each with step, key numbers, one next-action button. |
| Step ② | Layout B: one row per candidate market with profiles as chips ("+ add", popover for resumes), and a "What 'Score N applications' does" box. |
| Step ③ | Layout A: three plain actions (Export to sheet → Tailor N → Apply) and per-application Open / Resume / Q&A / Applied. |
| Export before tailoring | Every suggested job × profile is exported now; a missing resume shows "To tailor" / "Tailoring…" and the app fills the link into that row when the PDF is ready. |
| Export dialog | "Tailor the N jobs without a resume now" checkbox, on by default. |
| Generated Resumes | Q&A deep link from step ③; "From run: <sheet> ↗" link back. No "mark applied" there. |

### Out of scope

- Submitting applications automatically.
- Changing how jobs are checked, read or scored.
- Notifications when a run finishes.

## 2. Step track

`StepTrack({ current: 1 | 2 | 3, hint?: string })` (frontend): ① Check jobs → ② Pick profiles → ③ Tailor & apply,
current step highlighted, an optional one-line "what happens next" under it.

- Landing page: in the header. With no runs it is shown large, each step with one sentence:
  ① "Paste a sheet. We open every link and drop closed, old, on-site and clearance jobs."
  ② "Jobs are grouped by candidate location (US · UK/EU · Latam). Choose who applies where."
  ③ "Best resume per job, tailor the rest, export to your sheet, mark applied."
- Run page: in the header; step from the run: active + `phase="screen"` → 1, `screened` → 2, `phase="score"` → 3.

## 3. Landing page

- "+ New run" toggles the sheet panel (today's `NewRunPanel`). Open by default when the user has no runs.
- Runs are cards in three groups (each hidden when empty):
  - **Needs you**: `screened` runs, and `phase="score"` runs that are done with applications to go.
  - **In progress**: `queued` / `running` (either phase).
  - **Finished**: everything else (done with nothing to go, failed, cancelled).
- A card shows: sheet name (+ link to the sheet when `sourceUrl`), a step badge, key numbers, created date and
  "expires in N days", and one button:
  - step ①: "Checking 64 / 150" progress (no button; the card links to the run);
  - step ②: "150 links → 6 worth applying · US 3 · UK/EU 6 · Latam 1 · 71 closed", button **Review & pick profiles**;
  - step ③: "12 to apply · 4 applied · 2 tailoring" with a progress bar, button **Continue applying**;
  - finished: "31 applied of 34", a ⋯ menu with Delete (today's delete, with its confirm).
- Data: `GET /job-applies/runs` adds `summary` per run, from one aggregation over the user's rows (no per-row
  application logic): `worth`, `closed`, `markets` ({US, UKEU, LATAM} job counts via `markets_for(groupKey)`),
  `toApply` (suggested rows with no applied mark), `applied` (rows with an applied mark), `tailoring` (tailored map
  entries whose ResumeApplication is queued / in progress). Counts are per job, not per application, and are labelled
  "jobs".

## 4. Step ② — market rows

Replaces the market cards and the per-profile resume footer in `ScreeningReport` (the bucket summary, bucket lists,
max age and CSV export above stay).

- One row per market in `report.groups` order (United States, UK / EU, Latam, then "Location not stated" when
  present): "{market} · N jobs (M need a check)", then the picked profiles as chips, then "+ add".
  - Chip: profile name, its resume mode ("2 resumes" / "✨ tailor only"), ✕ to remove.
  - Chip popover: the profile's resume checkboxes (today's `ResumeChecklist` rules: at least one stays on), "Can also
    be tailored" / "No HTML template" hint. Resume choices are per profile, shared by every market the profile is in.
  - "+ add": a menu of usable profiles; fitting ones first, others marked "doesn't fit".
  - A market with jobs and no profile is highlighted: "No profile · + add".
- Picks start as today (fitting usable profiles) and keep the same state (`picks`, `unchecked`).
- "What 'Score N applications' does" box above the button, computed from the picks:
  - one line per picked profile: "{name}: best of {k} resumes on {n} {markets} jobs" or "{name}: tailored resumes
    for {n} {markets} jobs" (tailor-only);
  - "Next: step ③ shows the best resume per job; tailor the rest, export to your sheet, mark applied.";
  - warnings: "N {market} jobs have no profile and will be skipped".
  - N = number of job × profile pairs picked (a job in two picked markets for the same profile counts once).
- Button: **Score N applications →** (disabled rules as today). Minimum score slider next to it, as today.
- Per-profile job counts are per job (deduplicated across markets), fixing today's double count.

## 5. Step ③

### 5.1 Action strip (`ApplyWorkflow`)

Three columns, in this order:
1. **Export to sheet** — "N rows now · M links fill in after tailoring" (enabled whenever there are suggested
   applications not yet exported, whatever their resume state).
2. **✨ Tailor N** — "for {profile names}" (today's tailor-all dialog).
3. **Apply** — "N ready now · M applied".

### 5.2 Application lines (`Suggestions`)

Each application line keeps its resume/tailored state and Applied toggle, and gets:
- **↗ Open** — the job posting (`row.url`, new tab);
- **↓ Resume** — today's download (uploaded or tailored), "soon" while tailoring;
- **💬 Q&A** — when the line has a tailored resume: opens `/resume/generated?job=<ResumeApplication id>` in a new
  tab with that resume's drawer open.

## 6. Export before tailoring (backend)

`POST /runs/{id}/export-sheet` (body gains `tailorMissing: bool = False`):

- Exports every application of the suggested view that is not applied and not yet exported, including
  `needsResume` and `tailoring`:
  - Resume cell: today's `=HYPERLINK(...,"Download")` when a resume is ready; otherwise the text "To tailor" or
    "Tailoring…"; Resume type: "Tailored" / "Uploaded · {filename}" / "Tailored (pending)".
  - Uploaded resumes without a stored PDF stay skipped (`noFile`), as today.
- `tailorMissing=True` queues tailoring for the exported `needsResume` applications (the same path as tailor-all,
  within its daily limit; the response reports `tailorQueued` and `overLimit`).
- Where each row went is recorded on the job row: `exportedRows.{accountId} = {sheetId, tab, row}` (row number from
  the append response's `updates.updatedRange`), alongside today's `exported.{accountId}`.
- Dry run returns the same counts plus `pendingLinks` (rows whose link fills in later).

### 6.1 Filling the link in later

When a tailored resume completes (`app/routers/resume.py` after `persist_job` for `source="job_applies"` with a
`jobApplyRowId`), and that job row has `exportedRows.{accountId}`:

1. Read cells A:J of the recorded row. If its Job URL (E) and Profile (B) match the job and profile, write the
   Resume (F) and Resume type (G) cells.
2. Otherwise search the tab's B and E columns for that job URL + profile; write there if found (and update the
   recorded row number).
3. Otherwise leave the sheet alone and log it. A Sheets failure never fails the resume job.

`google_sheets` gains `read_range(client, sheet_id, a1) -> list[list]` and `update_range(client, sheet_id, a1,
values)`, and `append_rows` returns the appended range.

## 7. Generated Resumes link

- Backend: the resume job list/detail (`_job_to_dict`) adds `jobApplyRowId`, `jobApplyRunId` and `jobApplyRunName`
  (looked up from the row's run; null when the run expired).
- `Generated.tsx`:
  - `?job=<id>` opens that resume's drawer on load (fetching it if it isn't on the current page).
  - A Job Applies resume shows "From run: {run name} ↗" linking to `/job-applies/{runId}`; when the run has
    expired, just "From a Job Applies run".

## 8. Error handling

- Sheet link fill-in failures are logged and skipped (§6.1); the export dialog says links fill in "when ready"
  without promising.
- `?job=` for a resume the user can't see or that was deleted: the page loads normally with a toast "That resume
  isn't available".
- Runs-list summary: a run whose aggregation fails shows its card without numbers.

## 9. Testing

- Backend unit: summary aggregation helpers (markets per job, toApply/applied counts); export row building for
  ready / needsResume / tailoring / noFile; updated-range parsing; the fill-in row match (same row, moved row, gone).
- Backend API (`tests/test_job_applies.py`, local test DB): runs list `summary`; export dry run and real run with
  placeholders (Google Sheets mocked), `tailorMissing`; fill-in after a tailored resume completes; resume job dict
  fields; `?job` lookup of another user's resume is not exposed.
- Frontend: `npm run lint`; browser run on the isolated stack: landing groups and buttons, step ② chips / popover /
  "What … does" box, step ③ strip and row actions, Q&A deep link opening the drawer, link back to the run.
