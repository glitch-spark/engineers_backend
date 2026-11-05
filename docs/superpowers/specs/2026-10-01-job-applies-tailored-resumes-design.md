# Job Applies — tailored resumes and choosing the resume you applied with

Date: 2026-10-01
Branches: `feat/job-applies` in `engineers_backend` and `engineers_app`
Builds on: `2026-10-01-job-applies-design.md` (scoring, runs, per-resume applied marks, permanent applied log)
Status: approved in conversation; pending spec review

## 1. Goal

When applying from a Job Applies run, the user applies with one of two kinds of file:

- **An uploaded resume.** This is one of the profile's PDFs from the Profiles page, usually a suggested match.
- **A tailored resume.** The existing Resume Generator writes it for this job: the profile's HTML template, plus the
  job description, plus the prompt chain.

Any job can be tailored, whether or not it has a matching resume. The user always chooses which file they applied
with, and the app records exactly that file. A batch of tailored resumes can be prepared ahead, so the apply loop
never waits. The design must be safe at 200+ applications a day: bounded concurrency, safe across restarts, and
capped cost.

### Decisions already made

| Topic | Decision |
|---|---|
| Unit of tailoring | One tailored resume per **job**, from **one profile**, because each profile has one HTML template. The default profile is the one that scored best on the job, even below the threshold; the user may pick another. Re-tailoring with a different profile replaces the job's tailored resume. |
| What counts as a bid | Job Applies generations count as a bid **only once the job is marked applied with that tailored resume**. Unmarking removes the bid. Generator-page generations keep counting when created, as today. |
| Generation pipeline | One persistent queue shared by the Generator page and Job Applies. It reuses `ResumeApplication` and `resume_service.generate_resume` (approach A). |
| Cover letters in batches | Off by default; a checkbox in the batch dialog turns them on. |
| Choosing the file | Any scored uploaded resume of any profile in the run, or the job's tailored resume. |
| Mark all | Uses the job's tailored resume when one is ready, otherwise the top suggested uploaded resume. |

### Out of scope

Editing tailored resumes in the app; generating several tailored versions per job; auto-applying; screening
answers in batches (still available per job on the Generated page).

## 2. Current state (facts this design relies on)

- `POST /resume/generate` (`app/routers/resume.py`):
  - creates a `ResumeApplication` (`status="queued"`)
  - then calls `asyncio.create_task(_run_job(...))`, with no task reference, no concurrency limit and no recovery
    after a restart
  - in `_run_job`:
    - the prompt chain is one-off → `account.resumePromptBody` → `owner.resumePromptBody` → system rules
    - it calls `generate_resume` (LLM, then Playwright PDF in a new Chromium per job, then S3)
    - then optional screening answers and a cover letter
- The S3 key is `{YYYY_MM_DD}/{profile}/{company}/Resume.pdf` (`resume_service._maybe_upload_to_s3`). Two jobs for
  the same profile, company and day overwrite one object.
- `generate_resume` reads only `Account.styleTemplate`, `pageFormat` and `name`. It never reads `Account.resumes`.
- Bids are counted as `ResumeApplication` documents by `createdAt`, whatever their status:
  - `app/routers/metrics.py`: `_bid_count`, `_series`, `_bids_by_user`, the weekly bid pipeline and `/dashboard-feed`
  - `app/alerts_service.py`: `_tick_daily_bids`
- The Generated page polls `GET /resume/jobs` every 3 s and auto-downloads newly completed jobs.
- Free-LLM 429s fall back to paid OpenAI silently (`llm_router.complete_with_fallback`).

## 3. Generation queue (backend, shared by both pages)

### 3.1 Model changes (`app/models/resume_application.py`)

```python
source: Literal["generator", "job_applies"] = "generator"
countsAsBid: Optional[bool] = None   # None/absent = counts (old records, Generator page); False = not yet a bid
leaseAt: Optional[datetime] = None   # worker heartbeat while generating
attempts: int = 0                    # claims so far; 3 → failed
promptOverride: Optional[str] = None # one-off prompt, kept for retries and restarts
jobApplyRowId: Optional[PydanticObjectId] = None
```

New index: `(status, createdAt)` for the queue scan.

### 3.2 Enqueue

The enqueue is shared and used by both pages:

```python
async def enqueue_generation(
    *, user, account, jd, company, job_url, questions, prompt_body, cover_letter,
    source="generator", job_apply_row_id=None,
) -> ResumeApplication
```

Validation is unchanged:
- the JD is at most 50 KB
- the account is owned by the user (admins bypass)
- the account has a template

It inserts the document with `status="queued"` and wakes the worker. `POST /resume/generate` calls it and no longer
creates a task itself. The one-off `promptBody` is stored on the document (`promptOverride`), so a retry or a
resume after restart keeps it.

### 3.3 Worker (`app/resume_queue.py`)

- **Loop:** one loop in the app lifespan, skipped when `RESUME_QUEUE_DISABLED=1` (tests). It wakes on enqueue
  (`asyncio.Event`) and every 5 s.
- **Claiming:** it claims jobs atomically with
  `find_one_and_update({status: "queued"}, {$set: {status: "in_progress", step: "generating_resume", leaseAt: now, startedAt: now}}, sort createdAt)`,
  oldest first, and keeps at most `RESUME_LLM_CONCURRENCY` jobs (default 10) running, claiming round-robin across users (fewest running first).
- **Rendering:** PDF rendering inside `generate_resume` is limited by a module-level semaphore,
  `RESUME_RENDER_CONCURRENCY` (default 2).
- **Steps:** each job runs the existing `_run_job` steps unchanged: generate, upload, optional screening, optional
  cover letter, done or failed.
- **Lease:** the heartbeat refreshes `leaseAt` every 30 s while a job runs.
- **Recovery:** on startup and every 2 min, jobs that are `in_progress` with `leaseAt` older than 3 min (or null)
  go back to `queued`. Each job has `attempts` (incremented on claim); after 3 attempts it is `failed` with
  "Generation was interrupted repeatedly".

### 3.4 Unique S3 key

The key becomes `{YYYY_MM_DD}/{profile}/{company}/{jobId}/Resume.pdf`, with `jobId` passed into
`generate_resume`. Existing documents keep their stored `s3Key`.

### 3.5 Daily cap

`RESUME_DAILY_CAP` (default 300) limits generations a user can enqueue per calendar day (`APP_TIMEZONE`), for both
sources. Single enqueues past the cap get 429 with the message *"Daily limit of 300 tailored resumes reached. It
resets at midnight."* Batch behaviour is in §5.

## 4. Bid counting

- The aggregations in `metrics.py` and `alerts_service._tick_daily_bids` add `{"countsAsBid": {"$ne": False}}` to
  their `ResumeApplication` match. Old documents have no field, so they still count.
- Job Applies generations are created with `countsAsBid=False`.
- Marking a job applied with its tailored resume sets `countsAsBid=True` on that document.
  Unmarking sets it back to `False`.
- The bid is counted on the tailored document's `createdAt`, as today, so the date doesn't move.

## 5. Job Applies backend

### 5.1 Row link

`JobApplyRow` gets:

```python
tailoredJobId: Optional[PydanticObjectId] = None
tailoredAccountId: Optional[str] = None
```

Tailoring a row that already has a tailored job:
- **Not yet applied with:** the old job is replaced, and deleted if it is still queued.
- **Already used in an applied mark:** the old job is kept and the row points to the new one.

### 5.2 Endpoints (`app/routers/job_applies.py`)

| Method | Path | Purpose |
|---|---|---|
| POST | `/job-applies/rows/{rowId}/tailor` | Body `{accountId?: str, coverLetter?: bool}`. `accountId` defaults to the best-scoring profile for the row and must be one of the run's selected profiles. Enqueues with `source="job_applies"`, the row's `jdText`, `company` and `url`, and `countsAsBid=False`. Returns the tailored status. 400 if the row has no description; 429 at the daily cap. |
| POST | `/job-applies/runs/{runId}/tailor-all` | Body `{accountId?: str, coverLetter?: bool}`. Covers every row in the To apply view (suggested, not applied) that has no tailored job or only a failed one, using the profile filter or each row's top suggestion's profile. Enqueues up to the remaining daily cap. Returns `{queued, skippedCap}`. |
| — | rows list and row detail | Add `tailored: {jobId, accountId, status, step, hasPdf, error}`, joined from `ResumeApplication` for the page's rows in one query. Rows also include `otherResumes`: every scored resume `{accountId, resumeId, filename, total}` not among the suggestions, for the "Other resume…" picker. |
| GET | `/job-applies/runs/{runId}` | Adds `tailoring: {queued, inProgress, ready, failed}` for the run's tailored jobs. |

### 5.3 Applied marks name the file

`AppliedMark` becomes:

```python
accountId: str
resumeId: Optional[str] = None       # uploaded resume
tailoredJobId: Optional[str] = None  # tailored resume (ResumeApplication id)
at: datetime
```

Exactly one of `resumeId` and `tailoredJobId` is set.

- **`PUT /rows/{id}/applied`** accepts either field.
  - **An uploaded resume** must be one the row scored.
  - **A tailored resume** must be the row's `tailoredJobId`, completed with a PDF.
  - Marking with a tailored resume sets its `countsAsBid` (§4).
- **Log:** `job_application_log` stores the same fields. Its unique key becomes `(userId, url, fileKey)`, where
  `fileKey` is `resumeId` or `"tailored:" + tailoredJobId`. The old index `user_url_resume` is listed in
  `STALE_INDEXES` and replaced by `user_url_file`, and existing log entries are back-filled with
  `fileKey = resumeId`.
- **`mark-top-applied`** (selected or all): uses the row's tailored resume when it's `completed` with a PDF,
  otherwise the top suggestion. Rows with neither are skipped.
- **Unmarking:** `unmark-applied` and Undo accept marks of both kinds.

## 6. Job Applies UI

### 6.1 The "Apply with" cell, per job

- **Uploaded resumes:** suggested uploaded resumes as today, each with an applied checkbox and a download.
- **"Other resume…":** a small menu listing the row's `otherResumes`, by profile and score. Picking one marks it
  applied.
- **Tailored line:**
  - **States:** *Tailor ▾* when there is none (the menu lists the run's profiles by score for this job; the default
    is the first) → *Queued* → *Generating…* → *Tailored · {profile}*.
  - **When ready:** it shows an applied checkbox, a download and *Re-tailor ▾*.
  - **On failure:** *Failed: retry*, with the error in a tooltip.

### 6.2 Batch, header and keyboard

- **Action bar:** **Tailor all to apply (N)**, with a confirm dialog that shows N, the daily cap left and the
  cover-letter checkbox.
- **Run header:** *Tailoring 12 / 40* while jobs are queued or in progress, polling every 3 s while any are
  pending.
- **Keyboard:**
  - `t`: tailor the highlighted job with the default profile
  - `d`: download the tailored PDF if it's ready, otherwise the top suggestion's uploaded PDF
  - `a`: mark applied with the tailored resume if it's ready, otherwise the top suggestion, then move to the next job

### 6.3 Generated page

Job Applies generations show a "Job Applies" tag. They're left out of the page's auto-download on completion, so
batches don't trigger mass downloads.

## 7. Error handling

- A failed generation never affects the row's score or marks; the row shows *Failed: retry*.
- Retry uses the existing `POST /resume/jobs/{id}/retry`, which now re-queues and keeps `promptOverride`.
- With the daily cap reached, tailor buttons are disabled with a tooltip that says when the cap resets.
- **Deleting a run:** queued tailored jobs for it are deleted; completed ones stay on the Generated page.
- **When a run expires** (the 3-day TTL), tailored jobs are untouched; they are ordinary Generated-page documents.

## 8. Testing

All with the LLM call, PDF render and S3 upload stubbed:

- **Queue:**
  - at most N run at once
  - a restart re-queues jobs with a stale lease
  - after 3 attempts a job fails
  - `promptOverride` survives a retry
  - the unique S3 key is used
- **Daily cap:** a single enqueue past the cap returns 429; `tailor-all` queues up to the cap and reports
  `skippedCap`.
- **Bids:**
  - Generator-page jobs count
  - unmarked Job Applies jobs don't count, in metrics or in the daily-pace alert
  - marking applied with the tailored resume counts it, and unmarking removes it
- **Job Applies:**
  - tailoring defaults to the best profile and rejects profiles outside the run
  - re-tailoring replaces the job
  - marking with a tailored or "other" uploaded resume writes the right log entry
  - `mark-top-applied` prefers a ready tailored resume
  - the log back-fill and the unique index hold
- **Frontend:** typecheck and build, plus a manual pass on a run: tailor, wait, download, mark applied, Undo.
