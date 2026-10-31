# Statistics → Callers (caller interviews and pass rate)

Date: 2026-10-30 · Branch: `feat/caller-stats` (on `feat/bids-per-user-chart`: needs its shared filter bar and
custom range) · Frontend: `engineers_app`, same branch name

## Goal

Callers are paid by how many of their interview rounds pass. A Callers tab on Statistics shows, for a period, each
caller's interviews held in it and each one's result, so a week's interviews are easy to count. The layout is what
a later PDF export will send the caller.

Out of scope:
- pay amounts and pay status;
- the PDF export itself (its future shape is noted below);
- any automatic "no response" timeout. An awaiting round is rejected by hand (see "Mark rejected").

## Who sees it

Admins and interview managers (`routers/callers.is_caller_manager`), as it is about paying callers. Others don't get
the tab, and both endpoints return 403 to them.

## Rounds and sections

A caller round is a `stageHistory` entry with `caller.enabled`. Its caller is `caller.callerId`; rounds without one
are **Unassigned**. Its date is `scheduledAt` (falling back to `at`), as an app-local day (`dashboard_stats.app_tz`).

**A round counts in the period it was held, with its result as it is now.** A late result updates the period the
round was held in.

| Section | Rounds | Result shown |
|---|---|---|
| **Period** (first table) | held within the period | Passed, Rejected, Canceled, or Awaiting result (status completed) with the days waited |
| **Outdated pending** | held before the period, still awaiting their result | days waited |
| **Scheduled** | status scheduled (or none), dated on or before the period's last day | flagged "date passed, status not updated" once the round's end has passed |

A round already turns passed when the next round is added (`routers/interviews.mark_previous_passed`). So
"Awaiting" means held, with no next round and no result entered.

- **Pass rate** = passed ÷ (passed + rejected) in the Period table. It is `null` (shown "–") when none is decided.
- **Interviews** (the count on a card) = passed + rejected + awaiting in the Period table. Canceled rounds are listed
  but not counted.

The region is the interview profile's (`Account.region`), as on the Interview rate tab; All means every region.

## Layout (frontend)

The tab is the third on Statistics, **Callers**, after Interview rate and Resume compare. It uses the shared sticky
`FilterBar` (region, Week / Month / Custom).

1. **Caller cards.** There is one per caller in the caller list (`/callers`, in the caller's colour), plus
   **Unassigned** when it has rounds.
   - Each card shows the number of interviews with the canceled count in parentheses ("10 (2 canceled)"), the pass
     rate, ✓ passed · ✗ rejected · awaiting, and the outdated pending and scheduled counts.
   - An **All callers** card comes first, with every caller's counts added up. It starts selected, and its tables
     list every caller's rounds with a Caller column.
   - Clicking a caller's card narrows the tables to that caller. Callers with nothing are dimmed.
2. **Period table:** "‹Caller› · ‹period label›".
   - A **Sort: Date | Company** toggle (company: A–Z, then date), shared by all three tables.
   - Columns: Date · Company · Round · Engineer · Result, plus **Mark rejected** on awaiting rounds.
   - A totals line: Interviews · Passed · Rejected · Awaiting · Canceled (when any) · Pass rate.
3. **Outdated pending:** Date · Company · Round · Engineer · Waiting · Mark rejected.
4. **Scheduled:** Date · Company · Round · Engineer, with the stale flag.

Mark rejected asks for confirmation, then reloads the tab.

### Later: PDF export

A per-caller **Export PDF** button with an **Include scheduled rounds** checkbox. The PDF is the Period table, plus
the Scheduled section when the box is ticked. The endpoint already returns every section, so the export does no
counting of its own.

## Backend

- `app/caller_stats.py` holds the counting and no routes.
- `app/routers/metrics.py` holds the report route.
- `app/routers/interviews.py` holds Mark rejected, beside the other round routes. It already imports the
  caller-manager check; the callers router can't import the interview helpers without an import cycle.

### `GET /metrics/caller-stats?period=week|month|custom&date=YYYY-MM-DD&to=YYYY-MM-DD&region=all|Latam|EU|US`

- Dates are checked by the existing `_stats_days`; a bad custom range returns 400.
- It returns all callers at once, so switching cards needs no new request. Sorting is done in the browser.

```json
{
  "period": "week", "region": "all", "start": "2026-10-26", "end": "2026-11-01", "label": "Oct 26 – Nov 1, 2026",
  "callers": [
    {"callerId": "…", "name": "Muhammad", "color": "sky",
     "counts": {"passed": 1, "rejected": 1, "canceled": 0, "awaiting": 5, "outdated": 2, "scheduled": 2},
     "passRate": 0.5}
  ],
  "rounds": [
    {"interviewId": "…", "roundId": "…", "callerId": "…", "section": "period", "roundDate": "2026-10-27T15:00:00Z",
     "company": "ABB", "stage": "intro", "engineer": "Puma", "result": "awaiting", "daysWaiting": 3, "stale": false}
  ]
}
```

- `section` is one of `period | outdated | scheduled`.
- `result` is one of `passed | rejected | canceled | awaiting | scheduled`.
- `daysWaiting` is set on awaiting rounds; `stale` is true on scheduled rounds whose end has passed.
- `callerId` is `null` for Unassigned. Its `callers` entry has `callerId: null` and the name "Unassigned".
- Callers come in caller-list order, then callers no longer on the list that have rounds, then Unassigned.

### `POST /interviews/{interview_id}/stages/{round_id}/reject`

- For caller managers only, on any user's interview: interview managers can't edit other users' interviews through
  the interview routes.
- It only applies to a round awaiting its result (status `completed`). Any other status returns 409, and an unknown
  round returns 404.
- It sets the status to rejected, saves, and runs the same #caller sync as a round edit (`_sync_caller_posts`), so
  the Slack thread gets its usual "ended" reply.

## Old rounds with a typed caller name

Rounds saved before the caller list existed have only `callerName` (for example 7 "Pietro", 6 "Muhammad" and 7
"Mohammad"), so they would land in Unassigned. `scripts/link_typed_callers.py` sets their `callerId` once:

- It matches `callerName` to a caller's name ignoring case and spacing, plus an alias list (`mohammad` → Muhammad).
- It leaves "TBD", "Anyone" and unknown names (for example "David") as they are.
- By default it is a dry run that prints each change and the left-over names. It writes only with `--apply`, which
  is run after the printout is reviewed.

## Testing

Unit tests use mongomock, like `tests_unit/test_interview_rates.py`:

- Sections:
  - rounds held in the period, awaiting included;
  - older awaiting rounds go to Outdated pending, older decided ones are left out;
  - scheduled rounds, and the stale flag;
  - rounds held after the period are left out.
- Counts and pass rate per caller, `null` when nothing is decided; region filter; Unassigned rounds.
- Mark rejected:
  - works on an awaiting round;
  - 409 when the round isn't awaiting, 404 for an unknown round;
  - 403 for someone who isn't a caller manager;
  - calls the Slack sync.
- 403 on the report for someone who isn't a caller manager; 400 for a bad custom range.
- The script matches by name and alias and leaves TBD and unknown names.

Then the tab is checked in the browser against real data (week, month, custom; both sorts; all three tables).
