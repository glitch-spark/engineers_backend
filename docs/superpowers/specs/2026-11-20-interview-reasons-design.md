# Interview round reasons, reschedule log and backfill list — design

Date: 2026-11-20. Agreed with the user in conversation; the form wording is theirs. First part of the interview
statistics work: capture the data so people can backfill it. The statistics tab itself is a later project.

## Why

Interview statistics need three things the data does not hold today (checked on prod, 1,018 interviews):
- the result of a held round: 338 interviews stop at "completed" with no pass or reject;
- why a round was canceled or rejected: a free-text note exists on 18 of 559 rounds and cannot be counted;
- reschedules: a new time overwrites the old one, nothing is kept.

## Goals

1. A fixed reason (dropdown) on canceled, rescheduled and rejected rounds, with optional details.
2. Every reschedule is logged (old time, new time, who, when, reason).
3. A "Needs your input" list so owners can backfill results and reasons for rounds since Oct 6, 2026.

## Non-goals

The statistics tab; "no answer after 14 days" counting; offer / tech stack fields; changing how statuses work.

## Reasons (keys are the contract; labels verbatim)

Canceled and rescheduled share one list. Question: "Why was it canceled?" / "Why was it rescheduled?".

| key | label | helper text |
|---|---|---|
| `company` | Company | They canceled or moved it, or the interviewer didn't join. |
| `me` | Me | I didn't confirm the time clearly with the company or the caller. |
| `caller` | Caller | The caller had a schedule conflict or a personal matter. |
| `job_mismatch` | Job doesn't match | The location, no remote work or another requirement ruled it out. |
| `technical` | Technical issue | Audio, video, internet or the meeting link failed. |

Rejected. Question: "Why was it rejected?".

| key | label |
|---|---|
| `no_reason` | No reason given |
| `no_answer` | No answer from the company |
| `location` | Location, work permit or clearance |
| `language` | Language or communication |
| `technical_answers` | Weak technical answers |
| `experience` | Experience didn't match (stack, seniority, domain) |
| `background` | Slip in the background story |
| `test` | Failed test or take-home |
| `pay` | Pay or contract terms |
| `other_candidate` | Another candidate chosen or position filled |
| `av` | Audio or video problem |
| `other` | Other |

The free-text field stays (`statusNote`), labelled "Details (optional)" next to a reason.

## Data (backend, `InterviewStageEvent`)

- `statusReason: Optional[str] = None` — a key from the list of the round's status (canceled or rejected); None for
  any other status. Cleared when the status changes and no new reason is sent (same rule as `statusNote`).
- `reschedules: List[Reschedule] = []` — `{at, by, fromAt, toAt, reason, note}`; `reason` is a canceled/rescheduled
  key. Append-only; old data has none (cannot be backfilled).
- No migration: both default to empty on existing rounds.

## API (backend)

- Round JSON (every place a round is serialised): adds `statusReason` and `reschedules`.
- `RoundIn` (create interview, add round) and `StagePatch`: accept `statusReason`. Unknown key, or a key from the
  wrong list for the resulting status → 400 `invalid statusReason`. A reason sent with a status that takes none → 400.
- `StagePatch`: accepts `rescheduleReason` and `rescheduleNote`. When `rescheduleReason` is sent and `scheduledAt`
  changes the round's start, append a `Reschedule`. Sent without a time change → 400. A time change without
  `rescheduleReason` is saved as today and logs nothing (a typo fix, or an old client). Unknown key → 400.
- `GET /interviews/needs-input?userId=` — rounds scheduled on or after 2026-10-06 (app timezone; reuse
  `app.stats.conversion_scope.DATA_START`) that need something from the owner (`Interview.createdBy`):
  - `need: "result"` — the interview's latest round is `completed` and it has ended;
  - `need: "reason"` — a `canceled` or `rejected` round with no `statusReason`.
  Response: `{items: [{interviewId, roundId, companyName, profileName, ownerId, ownerName, stage, status,
  scheduledAt, need}], counts: {result, reason}}`, newest first. A normal user gets their own; an admin gets
  everyone's, or one person's with `userId`. AI interviews and home assessments are left out of `result`
  (they are excluded from statistics everywhere).
- Slack forms (`app/coordination/modals.py`): cancel, reschedule and reject forms get a required reason select with
  the labels above, saved through the same round update (`statusReason`, or `rescheduleReason` for a reschedule).
  The existing text input stays as the optional details.

## App

- `StatusNoteDialog` (asked when a round is set to Canceled / Rejected from the panel or the list) and the round
  editors that show the status note (`RoundFields`, `InterviewForm`): a required "reason" select above the note for
  canceled and rejected; Passed keeps only the note. The cancel list shows the helper text of the picked option.
- Moving the time of an existing scheduled round in the app asks "Why was it rescheduled?" with the five options
  plus "Just fixing the time" (saves without logging). Applies wherever an existing round's time is saved.
- Reason shown wherever the status note is shown today (label, then the details).
- Interviews page: a "Needs your input (N)" section at the top, hidden when empty. Each row: company · profile ·
  stage · date and what is missing. `result` rows offer Passed / Rejected (Rejected asks the reason, including
  "No answer from the company"); `reason` rows open the reason dialog. Admins see the owner's name on each row.
  Saving a row removes it from the list.

## Approaches

1. **Fixed keys on the round + reschedule log on the round (chosen).** Small, no migration, reuses the existing
   status dialog, Slack forms and round PATCH.
2. Free-text reasons classified later by an LLM — no new typing, but 18 notes exist; nothing to classify.
3. A separate events collection for every status / time change — a full audit trail, far more than the statistics
   need.

## Calls made by the builder (not the user's words)

- "No answer from the company" is a rejected reason, so a ghosted round can be closed from the backfill list.
- "Just fixing the time" exists so a typo correction is not counted as a reschedule.
- The reason is required in the app dialogs and Slack forms, optional in the API (old clients keep working).
- Labels live in both repos (the Slack forms need them in the backend); keys are the shared contract.

## Constraints

No runs against the real database (local dev points at prod); tests use the existing mongomock setup. Commits
single line, no co-author or "Generated with" trailers. TDD. Backend: `pytest tests_unit` with the usual three
ignored files (2 known local failures). App: `npx vitest run`, `npx tsc --noEmit`.
