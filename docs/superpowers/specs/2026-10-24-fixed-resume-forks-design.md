# Fixed-resume forks in Job Applies

Date: 2026-10-24 · Branch: `feat/fixed-resume-forks` · Follows: 2026-10-23-interview-resume-link-design.md

## Goal

When scoring picks a profile's uploaded ("fixed") resume for a job, that pick becomes a per-job record like a
tailored resume: its own copy of the file, the job description, the resume's score with the job, its own download and
sheet link. Fixed applications then show on the Generated page, in the interview company search and in statistics the
way tailored ones do.

## Today

Scoring stores each profile's best uploaded resume per job on the run row (`bestByProfile`, deleted with the run
after 7 days). At or above the run's minimum, the job is "ready" with that resume and nothing is recorded; the sheet
link points to the profile's shared file.

## Design

**The fork** is a `ResumeApplication` (the tailored-resume collection) with `fixedFrom = <profile resume id>`,
`source: job_applies`, `status: completed`, `countsAsBid: False`, `jobApplyRowId`, the job's company / URL / JD,
`plainText` = the resume's text, `matchScore` = its run score (`tailored_score.match_from_score`), `pdfFilename` = the
resume's file name and `s3Key` = a server-side S3 copy of the resume's file (a real fork: it survives the profile
resume being replaced). Only PDF resumes with a stored file are forked; others keep today's shared-file behaviour.

**The row points to it in `row.tailored[accountId]`**, the tailored resume's slot, so download, bulk download, sheet
export, mark-applied (and so bid counting) work unchanged. `_tailored_out` carries `fixedFrom` for the UI.

**Sync** (`app/job_apply/fixed_forks.py: sync_row`), after a row is scored and when the run's minimum changes:
- the slot holds a real tailored resume → nothing (it wins);
- the best uploaded resume is at or above the minimum, the profile passes the job's gates, it is a stored PDF:
  same resume as the current fork → refresh its score; else create a fork (compare-and-set on the slot) and retire
  the previous fork;
- otherwise a fork in the slot is retired.

Retiring deletes a fork unless it is in use (marked applied, exported to a sheet, or linked to an interview); a fork
in use stays in the slot. Tailoring the job later replaces the slot as today; the fork stays as history.

**Not a generation:** forks are left out of the daily generation cap (`resume_queue.daily_count`) and the run's
tailoring counters. They count as bids once marked applied, like tailored resumes.

**Interview link:** the company search finds forks as generated resumes; they show and snapshot as `kind: fixed`.
The form's "Used a fixed resume" reason and picker are removed (forks replace them); the value stays readable for the
interviews already saved with it, and new requests with it are refused.

**UI:** Job Applies shows a **Fixed** chip (score, download, select) where a tailored one would be; the Generated page
shows a **Fixed** badge with the source file name; the interview pages label these links "Fixed".

## Existing data

`scripts/backfill_fixed_forks.py` (dry run by default) syncs every scored row of the live runs. Run it before the
interview backfill so interviews can link to forks.

## Testing

mongomock-motor unit tests with S3 copy faked: fork created with score / JD / file, same pick refreshed, new best
resume switches the fork, below the minimum retires it (kept when in use), a tailored resume is never touched, non-PDF
skipped, daily cap ignores forks, threshold change syncs, mark-applied makes it a bid, interview search returns
`kind: fixed`. Frontend: `tsc` and a manual run.
