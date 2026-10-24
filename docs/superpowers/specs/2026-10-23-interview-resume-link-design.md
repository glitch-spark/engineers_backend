# Interview ↔ resume link (capture for interview statistics)

Date: 2026-10-23 · Branch: `feat/interview-statistics` · Part A of 2 (part B: the statistics view, its own spec)

## Goal

Every interview records which resume and job it came from: the tailored or uploaded resume, the job description
and the ATS match score, frozen at link time. Later, statistics can relate interviews to scores, tailored vs
uploaded resumes, prompts and models. When no resume can be found, the interview records why.

Applies to all interviews (normal and Caller): both use the one `InterviewForm` / `ApplicationFields`.

## Sources

| Source | What it gives | Notes |
|---|---|---|
| Generated resumes (`resumeapplications`, Generator + Job Applies) | company, JD, PDF, `matchScore`, prompt, model | completed only; JD/plainText archived to S3 after 30 days (`textArchiveKey`) |
| Job Applies applied log (`job_application_log`), uploaded-resume entries | company, URL, profile, resume file | from now on also `jdText` + `score`, written when marked applied |
| Applied outside the app | nothing | "No company found" + reason |

Bids are not a source: no JD, and attachments are usually just `Resume.pdf`.

## Data model

`Interview.resumeLink: Optional[InterviewResumeLink]`, one per interview (not per round):

- `kind`: `tailored | uploaded | none`; `linkedBy`: `user | auto`; `linkedAt`
- link: `resumeJobId` (tailored), `appliedLogId` + `resumeId` (uploaded), `matchedCompany`, `source`
  (`generator | job_applies`), `filename`, `promptName`, `resumeModel`, `appliedAt`
- snapshot: `jobDescription` (full text, restored from the S3 archive when stripped), `jobUrl`,
  `score` (`TailoredMatch`: total, band, knockouts, missing; null when never scored)
- `none` only: `reason` (`outside_app | recruiter | referral | name_differs | other`), `reasonNote`
  (required for `other`)

Reason `fixed_resume` ("Used a fixed resume"): the user picks which of the profile's uploaded resumes was sent. It is
saved as `kind: uploaded` with that `resumeId`/`filename` and `reason: fixed_resume`; its JD comes from the JD cache
for the interview's Job URL (normalised) and it is scored against it when found (`GET /interviews/fixed-resume` for
the form's preview).

`companyName` stays the interview's displayed company; picking a match sets it to the saved spelling (editable).

`JobApplicationLog` gains `jdText` and `score` (`{total, band, knockouts}` from the run's score for that uploaded
resume), written by `_set_applied`. Older entries without them fall back to the 30-day JD cache when linked.

## API

- `GET /interviews/resume-matches?accountId&q` — `_account_for_write` permission. Profile-scoped: completed
  generated resumes + uploaded applied-log entries whose normalised company contains normalised `q` (≥2 chars).
  Normalising: lower case, drop punctuation and suffixes (inc, llc, ltd, corp, co, gmbh, plc, the, …). Newest
  first, max 20: `{matchId, kind, company, appliedAt, filename, source, promptName, jobUrl, score}` (no JD: the
  preview loads it). `matchId` = `t:<resumeJobId>` | `u:<appliedLogId>`.
- `GET /interviews/resume-matches/{matchId}?accountId` — the full snapshot (with JD) for the preview card.
- `POST /interviews`, `PUT /interviews/{id}` — optional `resumeLink`: `{matchId}` or
  `{none: true, reason, note}`. The server validates the match belongs to the interview's profile and builds the
  snapshot itself (`linkedBy: user`). On create it is required when `INTERVIEW_RESUME_LINK_REQUIRED` is on (off
  until the new frontend is deployed). On edit it is optional; changing the profile drops a link to the old
  profile's resume. `reason: other` needs a note.
- Interview responses carry `resumeLink`; list responses leave out `jobDescription`.
- Linking a Generator resume that was never scored scores it then (`tailored_score.score_manual`, using archived
  text if needed).

## Form

`ApplicationFields` Company becomes a combobox (pattern of `CountrySelect`), enabled once a profile is picked,
searching after 2 chars (250 ms debounce). Options show company, tailored/uploaded, date and the `TailoredScore`
badge. Picking one sets Company (saved spelling), fills an empty Job URL, and shows a "Linked resume" card
(file, prompt, score + missing terms, job link, collapsible JD) with **Download** and **Change**. A "No company
found" checkbox (enabled once a name is typed) locks Company as typed and shows Reason + Note; "Used a fixed resume"
adds a resume dropdown and a preview card with Download. Required on create: a match or a
reason (+ note for Other). On Edit details, optional; changing Profile clears the link. The interview panel shows
one line: the linked resume and score, or the no-match reason.

## Backfill

`scripts/backfill_interview_resume_links.py`: dry run by default, `--apply` to write, `--score` to score unscored
Generator resumes (one cached LLM extraction per distinct JD). Interviews without `resumeLink`: candidates are the
same profile's generated resumes and uploaded applied-log entries whose normalised company **equals** the
interview's and dated before its first round; the most recent wins. `linkedBy: auto`. No match → left unlinked
(the reason is unknown). Archive files are fetched once per run.

## Testing

mongomock-motor unit tests, S3 and LLM faked: normalising and search, permissions, snapshot building (tailored,
uploaded, archived, no JD, no score), create/update validation, applied-log JD+score, backfill rules and
idempotence. Frontend: `tsc`, `missingFields`, manual run on the local copy.

## Rollout

1. Backend (requirement off). 2. Frontend. 3. Turn `INTERVIEW_RESUME_LINK_REQUIRED` on after the frontend deploys.
4. Backfill dry run → review numbers → `--apply`.
