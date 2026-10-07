# Bid review UX: focus viewer, real Submit evidence, profile/resume check

Date: 2026-10-06
Repos: `engineers_backend` (dev), `engineers_app` (main), `Engineer/Bid Track (BackBlaze)` (main)
Builds on: `2026-10-05-bid-records-review-design.md` (bid records, `/bids` API, Bid review page)
Status: design approved in conversation, pending spec review

## 1. Goal

Reviewers (bidder owners and admins) handle about 50–200 bids a day. Their two pains are **speed** (too many bids)
and **judging a bid from what is shown**. To approve a bid a reviewer must confirm two things:

1. **The application was submitted** — a screenshot of the submitted/confirmation step.
2. **The right profile/resume was used** — the form shows the bidder's assigned profile and its resume.

The review page puts exactly that evidence in front of the reviewer and lets them decide one bid after another with
the keyboard. Nothing is auto-approved or auto-rejected.

### Findings this fixes (browser test 2026-10-05/06)

- Every bid is flagged "No submit screenshot": the extension (3.1.0) only ever sends `trigger: "step"`.
- After the 18:30 Chicago report time the page opens on the just-reported day while new bids sit on the next day.
- "Approve all shown" counts rejected bids (status filter All) and would flip them to approved.
- A bid whose uploads were never confirmed shows empty screenshot slots.

### Out of scope

Duplicate detection, auto-flags/triage rules, bidder-facing review results in the extension, daily close-out of
reports, per-bidder analytics, editing posted Slack messages.

## 2. Extension: real Submit capture

- The popup gets a second button under **Upload Screenshot**: **Submit screenshot**. Bidders click it on the
  confirmation page after submitting (or on the final page just before Submit when there is no confirmation page).
- It runs the same capture as Upload Screenshot with `trigger: "submit"` (content script `requestPageCapture("submit")`
  via a new `CAPTURE_SUBMIT` message, or a `trigger` field on `CAPTURE_NOW`). The file name, step numbering, upload,
  confirm and local download are unchanged; the result line reads "✓ Submit (step N) saved · uploaded".
- README: one line on when to use Submit screenshot. Version → 3.2.0.
- Backend: no change — `/ext/upload-url` already accepts `submit` and `record_screenshot` sets `submittedAt`.

## 3. Evidence in the review API

`GET /bids` adds to each `BidOut`:

| Field | Meaning |
|---|---|
| `jobDomain` | Host of `jobUrl` without `www.` (e.g. `boards.greenhouse.io`); `null` without a URL. |
| `durationSec` | Seconds from the first to the last screenshot's `capturedAt`; `0` for one screenshot. |
| `profileName` | Name of the bidder's assigned profile (`Bidder.profileId` → `Account.name`); `null` if none. |
| `resumeNames` | File names of that profile's uploaded resumes (`Account.resumes[].filename`), in stored order. |
| `tailoredResumeName` | `pdfFilename` of the newest **completed** `ResumeApplication` for that profile (`accountId`) whose `jobUrl` equals the bid's `jobUrl` (exact string); `null` if none. |
| `rejectReason` | See §5; `null` unless rejected with a reason. |

Profiles and tailored resumes are looked up once per request (one query per collection), not per bid.

`GET /bids/{id}/screenshots` adds `isSubmit: bool` per screenshot (`trigger == "submit"`).

## 4. Focus viewer (review mode)

A **Review** mode on the Bid review page (`/bids/review?mode=focus&day=…&bidderId=…`), sharing the list's filters.
It starts at the first pending bid of the filtered day, oldest first.

**Layout**

- Left (~70%): the **Submit screenshot** large and scrollable; without one, the last screenshot with a yellow
  "No submit screenshot" banner. Below it a thumbnail strip of all steps in order, the Submit one labelled.
  `←`/`→` switch steps, `F` opens the current step full size in a new tab.
- Right panel: bidder; **profile name**, **resume names**, **tailored resume name** (or "No profile assigned");
  job title, company, `jobDomain` linking to `jobUrl`, steps count, duration, first-screenshot time; flags
  (No submit screenshot, Missing upload, New screenshots since review); current status and reviewer.
- Actions: **Approve** (`A`); **Reject** (`R`) reveals reason buttons *No submission* (`1`), *Wrong profile/resume*
  (`2`), *Not a job page* (`3`), *Duplicate* (`4`), *Incomplete* (`5`), *Other…* (`6`, opens a note field). A reason
  may carry an optional note.

**Flow**

- After a decision the viewer moves to the **next pending bid**; `J`/`K` move next/previous without deciding.
- Progress line: "Bid 4 of 12 pending · 30 approved · 2 rejected" for the filtered day.
- `U` undoes the last decision in this session (sets it back to its previous status via the review endpoint); each
  decision shows a toast with an Undo button.
- Before applying a decision the viewer re-fetches that bid; if its status changed since it was shown, it shows the
  new status and asks again instead of overwriting.
- When no pending bids are left: "All bids for <day> reviewed" with a link back to the list.
- The next bid's screenshots are prefetched. Shortcuts never fire while typing in an input, textarea or select, or
  while a dialog is open.
- A screenshot whose upload was never confirmed shows "Screenshot didn't upload" in its slot.

## 5. Reject reasons

`PUT /bids/{id}/review` and `POST /bids/review` accept `reason` with `status: "rejected"`:
`no_submission` | `wrong_profile` | `not_job_page` | `duplicate` | `incomplete` | `other`. `other` requires a
non-empty `note`; other reasons allow an optional note. `reason` is stored as `Bid.rejectReason` and cleared when the
status changes to `approved` or `pending`. Labels are shown in list rows and in the viewer.

## 6. List view and fixes

- **Day tabs**: "Today (in progress)" = the open daily window, "Previous day (reported)" = the last closed window,
  plus the date picker. `GET /bids` without `day` returns the open window again (reverting the 2026-10-05
  "today's date" default), so evening bids are visible by default. The window's boundary is shown in words
  ("Oct 5 6:30 PM → Oct 6 6:30 PM Chicago") using `BID_REPORT_TIMEZONE`, returned by the API as `timezone`.
- Rows add `jobDomain`, `profileName`, a Submit badge (or the No-submit flag) and the reject reason label.
- **"Approve all complete (N)"** replaces "Approve all shown": the server approves only bids that are `pending`, have
  `submittedAt` set and have every screenshot confirmed, among the ids sent — new endpoint behaviour behind
  `POST /bids/review` with `onlyComplete: true` (returns `{updated, skipped}`); the button counts those bids and
  confirms with the number. Rejected and incomplete bids are never touched.
- **"Review pending (N)"** opens the focus viewer for the current filters.

## 7. Edge cases

- Bidder without a profile: "No profile assigned"; resume lines omitted.
- No tailored resume for the job: line omitted. Several: the newest completed one.
- Two reviewers on one bid: the re-fetch check in §4; bulk approve only touches pending bids server-side.
- A bid gets new screenshots while open in the viewer: `changedSinceReview` / the re-fetch shows them.
- Old bids from extension 3.1.0 (no Submit): shown with the No-submit flag; reviewers can still approve them.

## 8. Testing

- Backend unit tests (mongomock): new `BidOut` fields (domain, duration, profile/resume names, tailored resume
  lookup incl. none/several/incomplete), `isSubmit`, reject reasons (validation, `other` needs a note, cleared on
  approve/pending), `onlyComplete` bulk approve (skips rejected, incomplete, unconfirmed), default day = open window.
- Frontend: `tsc -b` and a Vite build.
- End to end: rerun the browser sandbox (`scratchpad/e2e`): extension Upload + Submit screenshot → focus viewer →
  approve, reject with a reason, undo → list and API agree; check the screenshots.
